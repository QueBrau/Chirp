"""Offline protocol checks for real post-deletion provider passes."""
from dataclasses import replace
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest

from app.services.account_post_deletion_cleanup import CleanupClaim, _digest
from app.services.account_post_deletion_providers import (
    cleanup_temporary_media, cleanup_identifiable_analytics, PostDeletionProviders,
)
from app.services import account_provider_gcs as gcs
from app.services import account_provider_analytics as bq

USER = uuid.UUID("12345678-1234-4234-8234-123456789abc")
TABLE = "run_googleapis_com_stdout_20261012"
LATE_TABLE = "run_googleapis_com_stdout_20261017"


def claim(category="temporary_media"):
    scope = ({"bucket": "chirp-media", "tmp_prefix": f"tmp/{USER}/",
              "initial_snapshot_digest": "a" * 64} if category == "temporary_media" else {
        "project": "chirps-prod", "dataset": "chirp_analytics", "tables": [TABLE],
        "table_pattern": "run_googleapis_com_stdout_YYYYMMDD",
        "predicate_version": "user_id_analytics_v1", "exclusions": [],
    })
    return CleanupClaim(uuid.uuid4(), uuid.uuid4(), USER, category, uuid.uuid4(), scope, _digest(scope))


class Reader:
    bucket_name = "chirp-media"

    def __init__(self, objects):
        self.objects = dict(objects)
        self.calls = []
        self.replacement = False
        self.lost = False

    def list(self, prefix, *, deadline):
        self.calls.append(("list", prefix))
        return [SimpleNamespace(name=k, generation=v) for k, v in self.objects.items() if k.startswith(prefix)]

    def read(self, name, *, deadline):
        self.calls.append(("read", name))
        return SimpleNamespace(name=name, generation=self.objects[name]) if name in self.objects else None

    def delete(self, name, generation, *, deadline):
        self.calls.append(("delete", name, generation))
        assert self.objects[name] == generation
        del self.objects[name]
        if self.replacement:
            self.objects[name] = "99"
        if self.lost:
            raise TimeoutError("response lost")


def retention():
    return gcs.MediaRetentionReview("chirp-media", False, 604800, verified=True)


@pytest.mark.asyncio
async def test_tmp_pass_never_touches_published_or_other_accounts_and_stays_pending():
    c = claim()
    own = f"tmp/{USER}/first"
    preserved = {f"posts/{USER}/public": "2", f"avatars/{USER}/avatar": "3",
                 f"tmp/{uuid.uuid4()}/other": "4"}
    reader = Reader({own: "1", **preserved})
    result = await cleanup_temporary_media(c, reader=reader, retention=retention())
    assert reader.objects == preserved
    assert result.remaining_count == 0
    assert result.receipt_type == "deferred_cleanup"
    assert result.scope_sha256 == c.scope_sha256
    assert result.retained_with_reason == ("gcs_soft_deleted_versions",)
    assert [call for call in reader.calls if call[0] == "delete"] == [("delete", own, "1")]


@pytest.mark.asyncio
async def test_replacement_after_exact_generation_delete_remains_for_a_later_pass():
    c = claim()
    own = f"tmp/{USER}/first"
    reader = Reader({own: "1"})
    reader.replacement = True
    result = await cleanup_temporary_media(c, reader=reader, retention=retention())
    assert result.remaining_count == 1
    assert reader.objects == {own: "99"}
    assert [x for x in reader.calls if x[0] == "delete"] == [("delete", own, "1")]


@pytest.mark.asyncio
async def test_lost_delete_response_never_returns_a_success_receipt():
    reader = Reader({f"tmp/{USER}/first": "1"})
    reader.lost = True
    with pytest.raises(gcs.MediaProviderUnavailable):
        await cleanup_temporary_media(claim(), reader=reader, retention=retention())
    assert reader.objects == {}
    assert len([x for x in reader.calls if x[0] == "delete"]) == 1


@pytest.mark.asyncio
async def test_scope_and_bucket_drift_are_rejected_before_any_provider_read():
    c, reader = claim(), Reader({})
    with pytest.raises(ValueError):
        await cleanup_temporary_media(replace(c, scope_sha256="b" * 64), reader=reader, retention=retention())
    reader.bucket_name = "foreign-bucket"
    with pytest.raises(ValueError):
        await cleanup_temporary_media(c, reader=reader, retention=retention())
    assert reader.calls == []


class BigQuery:
    """Only exact parameterized account predicates remove this fixture's rows."""

    def __init__(self):
        self.rows = {TABLE: 2, LATE_TABLE: 1}
        self.jobs = {}
        self.deleted = []
        self.calls = []
        self.streaming = False

    def table(self, name):
        value = {"type": "TABLE", "tableReference": {"projectId": "chirps-prod", "datasetId": "chirp_analytics", "tableId": name},
                 "schema": {"fields": [{"name": "jsonPayload", "type": "RECORD", "fields": [
                     {"name": "analytics", "type": "BOOLEAN"}, {"name": "user_id", "type": "STRING"}]}]}}
        if self.streaming:
            value["streamingBuffer"] = {}
        return value

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/datasets/chirp_analytics"):
            return httpx.Response(200, json={"datasetReference": {"projectId": "chirps-prod", "datasetId": "chirp_analytics"}, "location": "US", "maxTimeTravelHours": "168"})
        if path.endswith("/tables"):
            return httpx.Response(200, json={"tables": [self.table(n) for n in self.rows]})
        if "/tables/" in path:
            return httpx.Response(200, json=self.table(path.rsplit("/", 1)[-1]))
        if path.endswith("/jobs"):
            body = json.loads(request.content)
            cfg = body["configuration"]
            query = cfg["query"]
            assert query["queryParameters"][0]["parameterValue"]["value"] == str(USER)
            assert "jsonPayload.user_id = @user_id AND jsonPayload.analytics IS TRUE" in query["query"]
            assert int(query["maximumBytesBilled"]) <= bq.MAX_BYTES // bq.MAX_TABLES
            name = query["query"].split("`")[1].split(".")[-1]
            if cfg["dryRun"]:
                return httpx.Response(200, json={"statistics": {"query": {"totalBytesProcessed": "10000000", "totalBytesProcessedAccuracy": "PRECISE"}}})
            job_id = body["jobReference"]["jobId"]
            assert job_id not in self.jobs
            affected = self.rows[name]
            if query["query"].startswith("DELETE"):
                assert query["queryParameters"][1]["parameterValue"]["value"] == str(affected)
                self.rows[name] = 0
                self.deleted.append((name, job_id))
            self.jobs[job_id] = (body, self.rows[name])
            return httpx.Response(200, json={**body, "status": {"state": "DONE"}, "statistics": {"query": {"numDmlAffectedRows": str(affected)}}})
        if "/queries/" in path:
            body, count = self.jobs[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={"jobReference": body["jobReference"], "jobComplete": True,
                "totalRows": "1", "schema": {"fields": [{"name": "remaining", "type": "INTEGER"}]},
                "rows": [{"f": [{"v": str(count)}]}]})
        raise AssertionError("unexpected protocol request")


@pytest.mark.asyncio
async def test_analytics_pass_cleans_new_dated_tables_and_later_pass_uses_new_jobs():
    c, provider = claim("identifiable_analytics"), BigQuery()
    result = await cleanup_identifiable_analytics(c, token="test-token", transport=httpx.MockTransport(provider))
    assert provider.rows == {TABLE: 0, LATE_TABLE: 0}
    assert {t for t, _ in provider.deleted} == {TABLE, LATE_TABLE}
    assert result.scope == c.scope
    assert result.request_id == str(c.request_id)
    assert result.receipt_type == "deferred_cleanup"
    first_ids = {j for _, j in provider.deleted}
    provider.rows[LATE_TABLE] = 1
    await cleanup_identifiable_analytics(replace(c, owner_token=uuid.uuid4()), token="test-token", transport=httpx.MockTransport(provider))
    assert provider.rows[LATE_TABLE] == 0
    assert first_ids.isdisjoint({j for _, j in provider.deleted[2:]})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["foreign_table", "streaming", "bad_scope"])
async def test_analytics_drift_refuses_before_destructive_job(kind):
    c, provider = claim("identifiable_analytics"), BigQuery()
    if kind == "foreign_table":
        provider.rows["unapproved_table"] = 1
    elif kind == "streaming":
        provider.streaming = True
    else:
        c = replace(c, scope_sha256="b" * 64)
    with pytest.raises((ValueError, bq.AnalyticsProviderUnavailable)):
        await cleanup_identifiable_analytics(c, token="test-token", transport=httpx.MockTransport(provider))
    assert provider.deleted == []
    if kind == "bad_scope":
        assert provider.calls == []


@pytest.mark.asyncio
async def test_factory_requires_explicit_provider_access():
    for c in (claim(), claim("identifiable_analytics")):
        with pytest.raises(ValueError, match="unconfigured"):
            await PostDeletionProviders()(c)
