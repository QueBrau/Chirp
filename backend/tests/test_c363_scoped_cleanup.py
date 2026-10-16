"""Real-PostgreSQL checks for exact-scope load-test cleanup and rollback safety."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import models
from app.db import get_session_factory
from loadtest.cleanup import CleanupError, CleanupScope, apply_cleanup, preview_cleanup, write_plan


def _body(kind: str, firebase_uid: str, seq: int) -> str:
    return f"LOADTEST {kind} from {firebase_uid} seq {seq:06d} - synthetic content, safe to delete"


async def _scope_for(setup, *, start: datetime | None = None, end: datetime | None = None):
    async with get_session_factory()() as session:
        identity = (await session.execute(text(
            "SELECT current_database(), current_user, c.campus_id "
            "FROM chapters c WHERE c.id = :chapter_id"
        ), {"chapter_id": setup.chapter_id})).one()
    now = datetime.now(timezone.utc)
    raw = {
        "schema_version": 1,
        "campus_id": str(identity[2]),
        "chapter_id": setup.chapter_id,
        "expected_database_name": identity[0],
        "expected_database_role": identity[1],
        "run_started_at": (start or now - timedelta(minutes=5)).isoformat(),
        "run_ended_at": (end or now + timedelta(minutes=4)).isoformat(),
        "users": [{"firebase_uid": setup.member.firebase_uid, "user_id": setup.member.id}],
    }
    return CleanupScope.from_dict(raw)


async def _seed_content(setup, *, created_at: datetime | None = None):
    at = created_at or datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        campus_id = await session.scalar(text(
            "SELECT campus_id FROM chapters WHERE id=:chapter"
        ), {"chapter": setup.chapter_id})
        post = models.Post(
            author_id=uuid.UUID(setup.member.id),
            campus_id=uuid.UUID(str(campus_id)),
            chapter_id=uuid.UUID(setup.chapter_id),
            body=_body("post", setup.member.firebase_uid, 1),
            audience="org",
            post_type="text",
            created_at=at,
        )
        chirp = models.Chirp(
            author_id=uuid.UUID(setup.member.id),
            campus_id=uuid.UUID(str(campus_id)),
            body=_body("chirp", setup.member.firebase_uid, 2),
            created_at=at,
        )
        session.add_all([post, chirp])
        await session.flush()
        comment = models.PostComment(
            post_id=post.id,
            author_id=uuid.UUID(setup.member.id),
            body=_body("comment", setup.member.firebase_uid, 3),
            created_at=at,
        )
        session.add(comment)
        await session.commit()
        return post.id, chirp.id, comment.id


@pytest.mark.asyncio
async def test_preview_is_read_only_exactly_scoped_and_full_body_bound(
    client, make_chapter_with, make_user, make_campus, tmp_path,
):
    setup = await make_chapter_with("president")
    other = await make_user("Unrelated author")
    other_campus = await make_campus()
    async with get_session_factory()() as session:
        target_campus = str(await session.scalar(
            text("SELECT campus_id FROM chapters WHERE id = :chapter_id"),
            {"chapter_id": setup.chapter_id},
        ))
        other_chapter = models.Chapter(campus_id=uuid.UUID(target_campus), org_name="Out of scope")
        session.add(other_chapter)
        await session.flush()
        other_chapter_id = str(other_chapter.id)
        await session.commit()
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=2), now + timedelta(minutes=2)
    selected = await _seed_content(setup, created_at=now)

    # Marker-like rows with a wrong UID, altered suffix, or an exclusive time bound
    # must remain outside the reviewed candidate set.
    async with get_session_factory()() as session:
        for seq, body, campus, chapter, at in (
            (4, _body("post", other.firebase_uid, 4), target_campus, setup.chapter_id, now),
            (5, _body("post", setup.member.firebase_uid, 5) + " extra", target_campus, setup.chapter_id, now),
            (6, _body("post", setup.member.firebase_uid, 6), target_campus, setup.chapter_id, end),
            (8, _body("post", setup.member.firebase_uid, 8), target_campus, other_chapter_id, now),
            (7, _body("chirp", setup.member.firebase_uid, 7), other_campus, None, now),
        ):
            if body.startswith("LOADTEST chirp"):
                session.add(models.Chirp(
                    author_id=uuid.UUID(setup.member.id), campus_id=uuid.UUID(campus),
                    body=body, created_at=at,
                ))
            else:
                session.add(models.Post(
                    author_id=uuid.UUID(setup.member.id), campus_id=uuid.UUID(campus),
                    chapter_id=uuid.UUID(chapter) if chapter else None, body=body,
                    audience="campus" if chapter is None else "org", post_type="text", created_at=at,
                ))
        await session.commit()

    scope = await _scope_for(setup, start=start, end=end)
    async with get_session_factory()() as session:
        before = {}
        for table in (models.Post, models.PostComment, models.Chirp):
            before[table.__tablename__] = list((await session.scalars(select(table.id).order_by(table.id))).all())
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    plan_path = tmp_path / "reviewed-plan.json"
    write_plan(plan, plan_path)
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    saved = json.loads(plan_path.read_text())
    assert saved["counts"] == {"posts": 1, "post_comments": 1, "chirps": 1}
    assert {row["id"] for row in saved["candidates"]["posts"]} == {str(selected[0])}
    assert {row["id"] for row in saved["candidates"]["post_comments"]} == {str(selected[2])}
    assert {row["id"] for row in saved["candidates"]["chirps"]} == {str(selected[1])}
    assert setup.member.firebase_uid not in plan_path.read_text()
    assert _body("post", setup.member.firebase_uid, 1) not in plan_path.read_text()
    async with get_session_factory()() as session:
        after = {}
        for table in (models.Post, models.PostComment, models.Chirp):
            after[table.__tablename__] = list((await session.scalars(select(table.id).order_by(table.id))).all())
    assert before == after


@pytest.mark.asyncio
async def test_apply_deletes_only_reviewed_ids_and_requires_exact_confirmation(
    client, make_chapter_with, make_user, tmp_path,
):
    setup = await make_chapter_with("president")
    unrelated = await make_user("Unrelated synthetic author")
    selected = await _seed_content(setup)
    async with get_session_factory()() as session:
        outsider = models.Post(
            author_id=uuid.UUID(setup.member.id),
            campus_id=uuid.UUID(str(await session.scalar(text(
                "SELECT campus_id FROM chapters WHERE id=:chapter"
            ), {"chapter": setup.chapter_id}))),
            chapter_id=uuid.UUID(setup.chapter_id),
            body=_body("post", setup.member.firebase_uid, 10) + " plus unrelated suffix",
            audience="org", post_type="text",
        )
        session.add(outsider)
        outsider_author_post = models.Post(
            author_id=uuid.UUID(unrelated.id),
            campus_id=uuid.UUID(str(await session.scalar(text(
                "SELECT campus_id FROM chapters WHERE id=:chapter"
            ), {"chapter": setup.chapter_id}))),
            chapter_id=uuid.UUID(setup.chapter_id),
            body=_body("post", unrelated.firebase_uid, 11),
            audience="org", post_type="text",
        )
        session.add(outsider_author_post)
        await session.commit()
        outsider_id = outsider.id
        outsider_author_post_id = outsider_author_post.id
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())

    with pytest.raises(CleanupError) as missing:
        await apply_cleanup(scope, database_url, path, "")
    assert missing.value.code == "confirm_digest_mismatch"
    result = await apply_cleanup(scope, database_url, path, saved["plan_digest"])
    assert (result.deleted_posts, result.deleted_comments, result.deleted_chirps) == (1, 1, 1)
    async with get_session_factory()() as session:
        assert await session.get(models.Post, selected[0]) is None
        assert await session.get(models.PostComment, selected[2]) is None
        assert await session.get(models.Chirp, selected[1]) is None
        assert await session.get(models.Post, outsider_id) is not None
        assert await session.get(models.Post, outsider_author_post_id) is not None


@pytest.mark.asyncio
async def test_apply_rejects_row_fingerprint_drift_without_deleting_anything(
    client, make_chapter_with, tmp_path,
):
    setup = await make_chapter_with("president")
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    async with get_session_factory()() as session:
        post = await session.get(models.Post, selected[0])
        post.body += " changed after preview"
        await session.commit()

    with pytest.raises(CleanupError) as changed:
        await apply_cleanup(scope, database_url, path, saved["plan_digest"])
    assert changed.value.code == "plan_mismatch"
    async with get_session_factory()() as session:
        assert await session.get(models.Post, selected[0]) is not None
        assert await session.get(models.PostComment, selected[2]) is not None
        assert await session.get(models.Chirp, selected[1]) is not None


@pytest.mark.asyncio
async def test_apply_rejects_membership_drift_before_any_delete(client, make_chapter_with, tmp_path):
    setup = await make_chapter_with("president")
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    async with get_session_factory()() as session:
        await session.execute(text(
            "UPDATE memberships SET status='inactive' WHERE user_id=:uid AND chapter_id=:chapter"
        ), {"uid": setup.member.id, "chapter": setup.chapter_id})
        await session.commit()

    with pytest.raises(CleanupError) as changed:
        await apply_cleanup(scope, database_url, path, saved["plan_digest"])
    assert changed.value.code == "scope_membership_mismatch"
    async with get_session_factory()() as session:
        assert await session.get(models.Post, selected[0]) is not None
        assert await session.get(models.PostComment, selected[2]) is not None
        assert await session.get(models.Chirp, selected[1]) is not None


@pytest.mark.asyncio
async def test_uid_regex_metacharacters_are_literal_and_apply_exactly(client, make_chapter_with, tmp_path):
    setup = await make_chapter_with("president")
    literal_uid = "uid-a.+b"
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE users SET firebase_uid=:uid WHERE id=:id"),
                              {"uid": literal_uid, "id": setup.member.id})
        await session.commit()
    setup.member.firebase_uid = literal_uid
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    assert saved["counts"] == {"posts": 1, "post_comments": 1, "chirps": 1}
    result = await apply_cleanup(scope, database_url, path, saved["plan_digest"])
    assert (result.deleted_posts, result.deleted_comments, result.deleted_chirps) == (1, 1, 1)
    async with get_session_factory()() as session:
        assert await session.get(models.Post, selected[0]) is None
        assert await session.get(models.PostComment, selected[2]) is None
        assert await session.get(models.Chirp, selected[1]) is None


@pytest.mark.asyncio
async def test_photo_or_media_candidate_is_refused(client, make_chapter_with):
    setup = await make_chapter_with("president")
    post_id, _chirp_id, _comment_id = await _seed_content(setup)
    async with get_session_factory()() as session:
        post = await session.get(models.Post, post_id)
        post.post_type = "photo"
        post.media_urls = ["https://example.invalid/retained-object"]
        await session.commit()
    scope = await _scope_for(setup)
    with pytest.raises(CleanupError) as unsafe:
        await preview_cleanup(scope, os.environ["DATABASE_URL"])
    assert unsafe.value.code == "unsafe_dependencies"
    async with get_session_factory()() as session:
        assert await session.get(models.Post, post_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["photo", "media", "duration"])
async def test_nontext_or_media_metadata_candidate_is_refused(client, make_chapter_with, mutation):
    setup = await make_chapter_with("president")
    post_id, _chirp_id, _comment_id = await _seed_content(setup)
    async with get_session_factory()() as session:
        post = await session.get(models.Post, post_id)
        if mutation == "photo":
            post.post_type = "photo"
        elif mutation == "media":
            post.media_urls = ["gs://private-bucket/private-object"]
        else:
            post.duration_sec = 7
        await session.commit()
    with pytest.raises(CleanupError) as unsafe:
        await preview_cleanup(await _scope_for(setup), os.environ["DATABASE_URL"])
    assert unsafe.value.code == "unsafe_dependencies"
    async with get_session_factory()() as session:
        assert await session.get(models.Post, post_id) is not None


@pytest.mark.asyncio
async def test_exact_comment_on_historical_qa_parent_is_removed_but_parent_is_retained(
    client, make_chapter_with, tmp_path,
):
    setup = await make_chapter_with("president")
    async with get_session_factory()() as session:
        campus_id = uuid.UUID(str(await session.scalar(text(
            "SELECT campus_id FROM chapters WHERE id=:chapter"
        ), {"chapter": setup.chapter_id})))
        parent = models.Post(
            author_id=uuid.UUID(setup.member.id), campus_id=campus_id,
            chapter_id=uuid.UUID(setup.chapter_id), body="QA post retained from earlier run",
            audience="org", post_type="text",
        )
        session.add(parent)
        await session.flush()
        comment = models.PostComment(
            post_id=parent.id, author_id=uuid.UUID(setup.member.id),
            body=_body("comment", setup.member.firebase_uid, 12),
            created_at=datetime.now(timezone.utc),
        )
        session.add(comment)
        await session.commit()
        parent_id, comment_id = parent.id, comment.id
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    assert plan.payload["counts"] == {"posts": 0, "post_comments": 1, "chirps": 0}
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    result = await apply_cleanup(scope, database_url, path, saved["plan_digest"])
    assert result.deleted_comments == 1
    async with get_session_factory()() as session:
        assert await session.get(models.Post, parent_id) is not None
        assert await session.get(models.PostComment, comment_id) is None


@pytest.mark.asyncio
async def test_plan_symlink_and_scope_overflow_fail_closed(client, make_chapter_with, tmp_path):
    setup = await make_chapter_with("president")
    await _seed_content(setup)
    scope = await _scope_for(setup)
    raw = scope.canonical()
    raw["users"] = [
        {"firebase_uid": f"uid-{n}", "user_id": str(uuid.uuid4())}
        for n in range(501)
    ]
    with pytest.raises(CleanupError) as too_many:
        CleanupScope.from_dict(raw)
    assert too_many.value.code == "invalid_scope"

    plan = await preview_cleanup(scope, os.environ["DATABASE_URL"])
    real_path, link_path = tmp_path / "real.json", tmp_path / "plan-link.json"
    write_plan(plan, real_path)
    link_path.symlink_to(real_path)
    saved = json.loads(real_path.read_text())
    with pytest.raises(CleanupError) as symlink:
        await apply_cleanup(scope, os.environ["DATABASE_URL"], link_path, saved["plan_digest"])
    assert symlink.value.code == "invalid_plan"


@pytest.mark.asyncio
async def test_candidate_limit_is_enforced(client, make_chapter_with, monkeypatch):
    import loadtest.cleanup as cleanup

    setup = await make_chapter_with("president")
    await _seed_content(setup)
    async with get_session_factory()() as session:
        campus_id = uuid.UUID(str(await session.scalar(text(
            "SELECT campus_id FROM chapters WHERE id=:chapter"
        ), {"chapter": setup.chapter_id})))
        session.add(models.Post(
            author_id=uuid.UUID(setup.member.id), campus_id=campus_id,
            chapter_id=uuid.UUID(setup.chapter_id), body=_body("post", setup.member.firebase_uid, 13),
            audience="org", post_type="text",
        ))
        await session.commit()
    monkeypatch.setattr(cleanup, "MAX_CANDIDATES_PER_TABLE", 1)
    with pytest.raises(CleanupError) as capped:
        await preview_cleanup(await _scope_for(setup), os.environ["DATABASE_URL"])
    assert capped.value.code == "candidate_limit_exceeded"


@pytest.mark.asyncio
async def test_cli_defaults_to_preview_and_sanitizes_invalid_apply_inputs(client, make_chapter_with, tmp_path):
    setup = await make_chapter_with("president")
    await _seed_content(setup)
    scope = await _scope_for(setup)
    scope_path, plan_path = tmp_path / "scope.json", tmp_path / "plan.json"
    scope_path.write_text(json.dumps(scope.canonical()))
    scope_path.chmod(0o600)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", "")
    ]))
    command = [sys.executable, "-m", "loadtest.cleanup", "--scope", str(scope_path)]

    preview = subprocess.run(command + ["--plan-out", str(plan_path)], env=env,
                             capture_output=True, text=True, timeout=15)
    assert preview.returncode == 0, preview.stderr
    output = json.loads(preview.stdout)
    assert output["status"] == "preview" and output["plan_written"] is True
    assert setup.member.firebase_uid not in preview.stdout
    assert _body("post", setup.member.firebase_uid, 1) not in preview.stdout
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600

    missing_confirmation = subprocess.run(
        command + ["--apply", "--plan", str(plan_path)], env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert missing_confirmation.returncode == 2
    assert json.loads(missing_confirmation.stdout) == {
        "status": "refused", "reason": "apply_requires_plan_and_digest"
    }

    bad_plan = tmp_path / "bad-plan.json"
    bad_plan.write_text('{"secret":"do-not-print"}')
    bad_plan.chmod(0o600)
    invalid = subprocess.run(
        command + ["--apply", "--plan", str(bad_plan), "--confirm-digest", "0" * 64],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout) == {"status": "refused", "reason": "invalid_plan"}
    assert "do-not-print" not in invalid.stdout + invalid.stderr

    invalid_env = env.copy()
    invalid_env["DATABASE_URL"] = "postgres://private:secret@localhost/ignored"
    invalid_db = subprocess.run(command, env=invalid_env, capture_output=True, text=True, timeout=15)
    assert invalid_db.returncode == 2
    assert json.loads(invalid_db.stdout) == {"status": "refused", "reason": "database_url_invalid"}
    assert "secret" not in invalid_db.stdout + invalid_db.stderr


@pytest.mark.asyncio
async def test_late_database_failure_rolls_back_prior_table_deletes(client, make_chapter_with, tmp_path):
    setup = await make_chapter_with("president")
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    trigger = "c363_fail_posts_" + uuid.uuid4().hex[:12]
    function = trigger + "_fn"
    async with get_session_factory()() as session:
        await session.execute(text(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "'BEGIN RAISE EXCEPTION ''c363 injected late delete failure''; END'"
        ))
        await session.execute(text(
            f"CREATE TRIGGER {trigger} BEFORE DELETE ON posts FOR EACH ROW EXECUTE FUNCTION {function}()"
        ))
        await session.commit()
    try:
        with pytest.raises(CleanupError) as failed:
            await apply_cleanup(scope, database_url, path, saved["plan_digest"])
        assert failed.value.code == "cleanup_outcome_unknown"
        async with get_session_factory()() as session:
            assert await session.get(models.Post, selected[0]) is not None
            assert await session.get(models.PostComment, selected[2]) is not None
            assert await session.get(models.Chirp, selected[1]) is not None
    finally:
        async with get_session_factory()() as session:
            await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON posts"))
            await session.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))
            await session.commit()


@pytest.mark.asyncio
async def test_cancellation_during_final_delete_rolls_back_transaction(
    client, make_chapter_with, tmp_path,
):
    setup = await make_chapter_with("president")
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())
    trigger = "c363_sleep_posts_" + uuid.uuid4().hex[:12]
    function = trigger + "_fn"
    async with get_session_factory()() as session:
        await session.execute(text(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "'BEGIN PERFORM pg_sleep(5); RETURN OLD; END'"
        ))
        await session.execute(text(
            f"CREATE TRIGGER {trigger} BEFORE DELETE ON posts FOR EACH ROW EXECUTE FUNCTION {function}()"
        ))
        await session.commit()
    task = None
    try:
        task = asyncio.create_task(apply_cleanup(scope, database_url, path, saved["plan_digest"]))
        query_seen = False
        async with asyncio.timeout(4):
            while not query_seen:
                async with get_session_factory()() as observer:
                    query_seen = bool(await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE state='active' AND query LIKE '%DELETE FROM posts p USING users u,selected s%' "
                        "AND pid <> pg_backend_pid())"
                    )))
                if not query_seen:
                    await asyncio.sleep(0.05)
        assert query_seen, "apply never reached the final posts DELETE"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with get_session_factory()() as session:
            assert await session.get(models.Post, selected[0]) is not None
            assert await session.get(models.PostComment, selected[2]) is not None
            assert await session.get(models.Chirp, selected[1]) is not None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with get_session_factory()() as session:
            await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON posts"))
            await session.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))
            await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", [
    "post_like", "chirp_vote", "report", "moderation_action", "foreign_comment", "foreign_parent_comment",
])
async def test_preview_fails_closed_on_retained_or_foreign_dependencies(
    client, make_chapter_with, make_user, dependency,
):
    setup = await make_chapter_with("president")
    foreign = await make_user("Foreign dependency author")
    post_id, chirp_id, _comment_id = await _seed_content(setup)
    async with get_session_factory()() as session:
        if dependency == "post_like":
            session.add(models.PostLike(post_id=post_id, user_id=uuid.UUID(foreign.id)))
        elif dependency == "chirp_vote":
            session.add(models.ChirpVote(chirp_id=chirp_id, user_id=uuid.UUID(foreign.id), value=1))
        elif dependency == "report":
            session.add(models.ContentReport(
                reporter_id=uuid.UUID(foreign.id), target_type="post", target_id=post_id,
                reason="preserve moderation evidence", status="open",
            ))
        elif dependency == "moderation_action":
            session.add(models.ModerationAction(
                actor_id=uuid.UUID(foreign.id), action="remove_content", target_type="post",
                target_id=post_id, reason="preserve moderation audit",
            ))
        elif dependency == "foreign_parent_comment":
            campus_id = uuid.UUID(str(await session.scalar(text(
                "SELECT campus_id FROM chapters WHERE id=:chapter"
            ), {"chapter": setup.chapter_id})))
            parent = models.Post(
                author_id=uuid.UUID(foreign.id), campus_id=campus_id,
                chapter_id=uuid.UUID(setup.chapter_id), body="Historical post by non-QA author",
                audience="org", post_type="text",
            )
            session.add(parent)
            await session.flush()
            session.add(models.PostComment(
                post_id=parent.id, author_id=uuid.UUID(setup.member.id),
                body=_body("comment", setup.member.firebase_uid, 14),
                created_at=datetime.now(timezone.utc),
            ))
        else:
            session.add(models.PostComment(
                post_id=post_id, author_id=uuid.UUID(foreign.id),
                body="A reply from another person", created_at=datetime.now(timezone.utc),
            ))
        await session.commit()
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    with pytest.raises(CleanupError) as unsafe:
        await preview_cleanup(scope, database_url)
    assert unsafe.value.code == "unsafe_dependencies"
    async with get_session_factory()() as session:
        assert await session.get(models.Post, post_id) is not None
        assert await session.get(models.Chirp, chirp_id) is not None


@pytest.mark.asyncio
async def test_report_insert_racing_apply_is_seen_before_any_delete(client, make_chapter_with, make_user, tmp_path):
    setup = await make_chapter_with("president")
    reporter = await make_user("Concurrent reporter")
    selected = await _seed_content(setup)
    scope = await _scope_for(setup)
    database_url = os.environ["DATABASE_URL"]
    plan = await preview_cleanup(scope, database_url)
    path = tmp_path / "plan.json"
    write_plan(plan, path)
    saved = json.loads(path.read_text())

    writer = get_session_factory()()
    task = None
    try:
        await writer.begin()
        writer.add(models.ContentReport(
            reporter_id=uuid.UUID(reporter.id), target_type="post", target_id=selected[0],
            reason="concurrent retained report",
        ))
        await writer.flush()
        task = asyncio.create_task(apply_cleanup(scope, database_url, path, saved["plan_digest"]))
        waiting = False
        async with asyncio.timeout(5):
            while not waiting:
                async with get_session_factory()() as observer:
                    waiting = bool(await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE state='active' AND wait_event_type='Lock' "
                        "AND query LIKE 'LOCK TABLE content_reports IN SHARE MODE%' "
                        "AND pid <> pg_backend_pid())"
                    )))
                if not waiting:
                    await asyncio.sleep(0.05)
        assert waiting, "apply did not acquire a report-insert fence"
        await writer.commit()
        with pytest.raises(CleanupError) as unsafe:
            await task
        assert unsafe.value.code == "unsafe_dependencies"
        async with get_session_factory()() as session:
            assert await session.get(models.Post, selected[0]) is not None
            assert await session.get(models.PostComment, selected[2]) is not None
            assert await session.get(models.Chirp, selected[1]) is not None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if writer.in_transaction():
            await writer.rollback()
        await writer.close()

