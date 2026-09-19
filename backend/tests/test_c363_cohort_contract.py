"""Refusals and private-output boundaries for the local cohort tools."""
from dataclasses import replace
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import sys
from uuid import uuid4

import pytest

from loadtest import cohort_fixture as fixture
from loadtest import cohort_workload as workload
from loadtest.cohort_contract import parse_manifest, validate_cohort_run
from loadtest.config import load_config
from loadtest.receipt_contract import ReceiptError
from loadtest.receipt_fixture import FixtureError


def manifest_data():
    result = {"schema_version": 1, "cohorts": []}
    for index, (members, rows) in enumerate(((100, 51), (300, 201))):
        user_ids = [str(uuid4()) for _ in range(members)]
        conversations = [str(uuid4()) for _ in range(rows)]
        users = [{"uid": f"receipt-cohort-{index}-{user}", "user_id": user_ids[user]} for user in range(3)]
        result["cohorts"].append({
            "receipt": {"schema_version": 1, "campus_id": str(uuid4()), "chapter_id": str(uuid4()),
                        "users": users, "message_workload": {
                            "sender_uid": users[0]["uid"], "sender_device_id": str(uuid4()),
                            "conversation_id": conversations[0],
                            "recipient_uids": [user["uid"] for user in users[1:]],
                        }},
            "dataset": {"member_ids": user_ids, "post_ids": [str(uuid4()) for _ in range(rows)],
                        "chapter_post_ids": [str(uuid4()) for _ in range(rows)],
                        "conversation_ids": conversations, "message_ids": [str(uuid4()) for _ in range(rows)],
                        "history_before": datetime.now(timezone.utc).isoformat()},
        })
    return result


def config():
    return load_config(Path(__file__).parents[1] / "loadtest/cohort-config.yaml")


def test_profiles_preserve_passive_and_active_denominators():
    cohorts = parse_manifest(json.dumps(manifest_data()))
    assert [len(c.member_ids) for c in cohorts] == [100, 300]
    assert [len(c.conversation_ids) for c in cohorts] == [51, 201]
    assert [len(c.receipt.http.users) for c in cohorts] == [3, 3]
    validate_cohort_run(config(), cohorts, 1, 4, 1)


@pytest.mark.parametrize("mutation,code", [
    (lambda raw: raw.update(schema_version=True), "cohort_schema_invalid"),
    (lambda raw: raw["cohorts"].pop(), "cohort_count_invalid"),
    (lambda raw: raw["cohorts"].__setitem__(1, raw["cohorts"][0]), "cohort_identity_overlap"),
    (lambda raw: raw["cohorts"][0]["dataset"]["member_ids"].pop(), "dataset_cardinality_invalid"),
    (lambda raw: raw["cohorts"][0]["dataset"]["member_ids"].__setitem__(0, str(uuid4())),
     "dataset_receipt_binding_invalid"),
    (lambda raw: raw["cohorts"][0]["dataset"]["post_ids"].__setitem__(0,
                 raw["cohorts"][0]["dataset"]["post_ids"][1]), "dataset_identity_duplicate"),
    (lambda raw: raw["cohorts"][0]["dataset"]["post_ids"].__setitem__(0,
                 raw["cohorts"][1]["dataset"]["post_ids"][0]), "cohort_identity_overlap"),
    (lambda raw: raw["cohorts"][0]["receipt"].update(chapter_id=raw["cohorts"][0]["receipt"]["campus_id"]),
     "cohort_identity_overlap"),
    (lambda raw: raw["cohorts"][0]["dataset"].update(history_before="private-invalid-timestamp"), "timestamp_invalid"),
])
def test_manifest_refuses_ambiguous_or_overlapping_fixture(mutation, code):
    raw = manifest_data()
    mutation(raw)
    with pytest.raises(ReceiptError, match=code):
        parse_manifest(json.dumps(raw))


@pytest.mark.parametrize("target", [
    "https://127.0.0.1:8010", "http://localhost:8010", "http://remote.invalid:8010",
    "http://127.0.0.1:8010/private", "http://127.0.0.1:8010?target=private",
    "http://remote.invalid@127.0.0.1:8010", "http://127.0.0.1:8010\n",
])
async def test_remote_and_ambiguous_targets_refuse_before_client_construction(monkeypatch, target):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input reached client/workload construction")
    monkeypatch.setattr(workload, "Shared", forbidden)
    with pytest.raises(ReceiptError, match="requires_literal_loopback_target"):
        await workload.run_cohorts(replace(config(), base_url=target), parse_manifest(json.dumps(manifest_data())))


@pytest.mark.parametrize("change", [
    lambda c: replace(c, ws_url="wss://remote.invalid/ws"),
    lambda c: replace(c, auth_mode="firebase"),
    lambda c: replace(c, caps=replace(c.caps, max_rps=21)),
    lambda c: replace(c, caps=replace(c.caps, max_concurrent_requests=11)),
    lambda c: replace(c, ws=replace(c.ws, max_sockets=3)),
    lambda c: replace(c, mix_weights={"feed_campus": 1}),
    lambda c: replace(c, ramp_in_seconds=1),
    lambda c: replace(c, duration_seconds=4),
])
def test_aggregate_contract_rejects_unapproved_shape(change):
    with pytest.raises(ReceiptError):
        validate_cohort_run(change(config()), parse_manifest(json.dumps(manifest_data())), 1, 4, 1)


@pytest.mark.parametrize("messages", [0, 4, True, 1.5])
def test_aggregate_message_bounds(messages):
    with pytest.raises(ReceiptError):
        validate_cohort_run(config(), parse_manifest(json.dumps(manifest_data())), messages, 4, 1)


@pytest.mark.parametrize("cohorts,recipients", [(1, 2), (5, 1), (2, 0), (3, 4), (4, 3), (True, 2), (2, True)])
def test_fixture_budgets(cohorts, recipients):
    with pytest.raises(FixtureError):
        fixture.profiles(cohorts, recipients)


def test_fixture_default_is_plan_only(tmp_path, monkeypatch, capsys):
    async def forbidden(*args, **kwargs):
        pytest.fail("plan touched a database")
    monkeypatch.setattr(fixture, "seed_database", forbidden)
    output = tmp_path / "untouched"
    monkeypatch.setattr(sys, "argv", ["cohort_fixture", "--out", str(output)])
    assert fixture.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PLAN_ONLY"
    assert not output.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_fixture_never_overwrites_before_database_work(tmp_path, monkeypatch, symlink):
    target = tmp_path / "existing"
    target.write_text("preserve")
    output = tmp_path / "manifest" if symlink else target
    if symlink:
        output.symlink_to(target)
    async def forbidden(*args, **kwargs):
        pytest.fail("existing output reached database writes")
    monkeypatch.setattr(fixture, "seed_database", forbidden)
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("AUTH_MODE", "emulated")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd")
    monkeypatch.setattr(sys, "argv", ["cohort_fixture", "--apply", "--out", str(output)])
    assert fixture.main() == 2
    assert target.read_text() == "preserve"


def test_fixture_success_writes_private_manifest(tmp_path, monkeypatch, capsys):
    raw = manifest_data()
    async def seed(*args):
        return raw
    monkeypatch.setattr(fixture, "seed_database", seed)
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("AUTH_MODE", "emulated")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd")
    output = tmp_path / "manifest"
    monkeypatch.setattr(sys, "argv", ["cohort_fixture", "--apply", "--out", str(output)])
    assert fixture.main() == 0
    assert json.loads(output.read_text()) == raw
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert raw["cohorts"][0]["receipt"]["campus_id"] not in capsys.readouterr().out


async def test_fixture_database_url_is_guarded_before_engine_construction(monkeypatch):
    import sqlalchemy.ext.asyncio
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine",
                        lambda *a, **kw: pytest.fail("remote database engine constructed"))
    with pytest.raises(FixtureError):
        await fixture.seed_database("postgresql+asyncpg://private@remote:5432/chirp_receipts_0123abcd", 2, 2)


@pytest.mark.parametrize("exception,code", [(RuntimeError, "cohort_task_failed"),
                                           (asyncio.CancelledError, "cohort_task_cancelled")])
async def test_owned_task_failure_cannot_be_hidden_by_complete_observation(monkeypatch, exception, code):
    async def failed_run(self):
        raise exception("private-task-detail")
    # A completed internal observation is insufficient when the task publishing
    # it fails. This isolates the outer coordinator's independent final gate.
    monkeypatch.setattr(workload.CohortObservation, "run", failed_run)
    monkeypatch.setattr(workload.CohortObservation, "report",
                        lambda self: {"status": "LOCAL_RECEIPTS_OBSERVED", "budgets": {}})
    monkeypatch.setattr(workload.CohortRunner, "coverage_complete", lambda self: True)
    report = await workload.run_cohorts(config(), parse_manifest(json.dumps(manifest_data())))
    assert report["status"] == "NOT_PROVEN"
    assert code in report["errors"]
    assert "private-task-detail" not in json.dumps(report)


async def test_caller_cancellation_finishes_all_owned_children_and_audit(monkeypatch):
    started, finished = set(), set()
    all_started = asyncio.Event()
    async def waiting_run(self):
        started.add(self.ordinal)
        if len(started) == 2:
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.add(self.ordinal)
    monkeypatch.setattr(workload.CohortObservation, "run", waiting_run)
    task = asyncio.create_task(workload.run_cohorts(config(), parse_manifest(json.dumps(manifest_data()))))
    await asyncio.wait_for(all_started.wait(), 1)
    task.cancel()
    report = await asyncio.wait_for(task, 1)
    assert report["status"] == "NOT_PROVEN"
    assert "run_cancelled" in report["errors"]
    assert started == finished == {1, 2}
    assert not any(task.get_name() in {"local-cohort", "cohort-driver-audit"}
                   for task in asyncio.all_tasks() if not task.done())


@pytest.mark.parametrize("symlink", [False, True])
def test_workload_existing_output_refuses_before_network(tmp_path, monkeypatch, symlink):
    manifest = tmp_path / "cohorts.json"
    manifest.write_text(json.dumps(manifest_data()))
    target = tmp_path / "existing"
    target.write_text("preserve")
    output = tmp_path / "report" if symlink else target
    if symlink:
        output.symlink_to(target)
    async def forbidden(*args, **kwargs):
        pytest.fail("existing output reached network work")
    monkeypatch.setattr(workload, "run_cohorts", forbidden)
    monkeypatch.setattr(sys, "argv", ["cohort_workload", "--config",
                        str(Path(__file__).parents[1] / "loadtest/cohort-config.yaml"),
                        "--manifest", str(manifest), "--out", str(output)])
    assert workload.main() == 2
    assert target.read_text() == "preserve"
