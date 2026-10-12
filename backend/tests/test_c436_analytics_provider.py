"""Offline BigQuery transport tests for the dormant analytics provider."""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from app.services.account_provider_analytics import (
    AnalyticsFreezeReceipt,
    AnalyticsProviderUnavailable,
    AnalyticsSnapshot,
    AnalyticsTable,
    BigQueryAnalyticsProvider,
    _digest,
)

USER = "12345678-1234-4234-8234-123456789abc"
PROJECT = "chirps-prod"
DATASET = "chirps_analytics"
TABLE = "run_googleapis_com_stdout_20261012"


def schema() -> dict:
    return {"fields": [{"name": "jsonPayload", "type": "RECORD", "fields": [
        {"name": "analytics", "type": "BOOLEAN"},
        {"name": "user_id", "type": "STRING"},
    ]}]}


def freeze() -> AnalyticsFreezeReceipt:
    return AnalyticsFreezeReceipt(USER, "freeze-c436", time.time() + 600, True, True)


def snapshot(table: str = TABLE, *, digest: str | None = None) -> AnalyticsSnapshot:
    current = AnalyticsSnapshot(
        "11111111-1111-4111-8111-111111111111", USER, PROJECT, DATASET,
        (AnalyticsTable(table, _digest(schema()), False),), "freeze-c436", "", "2026-10-12T00:00:00+00:00",
    )
    return AnalyticsSnapshot(**{**current.__dict__, "snapshot_digest": digest or _digest(current.payload())})


def provider(handler, *, snap: AnalyticsSnapshot | None = None, tables: tuple[str, ...] = (TABLE,)):
    return BigQueryAnalyticsProvider(
        token="operator-token", project=PROJECT, dataset=DATASET,
        snapshot=snap or snapshot(), freeze=freeze(), reviewed_tables=tables,
        transport=httpx.MockTransport(handler), timeout_seconds=5,
    )


def job_response(job_id: str, *, deleted: str = "1", rows=None, configuration=None) -> httpx.Response:
    body = {"jobReference": {"projectId": PROJECT, "jobId": job_id, "location": "US"},
            "status": {"state": "DONE"}}
    if configuration is not None:
        body["configuration"] = configuration
    if rows is not None:
        body["rows"] = rows
    else:
        body["statistics"] = {"query": {"dmlStats": {"deletedRowCount": deleted}}}
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_delete_binds_parameter_and_uses_no_redirects():
    calls = []
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["configuration"]["query"]["useLegacySql"] is False
            assert body["configuration"]["query"]["useQueryCache"] is False
            assert body["configuration"]["query"]["queryParameters"][0]["parameterValue"]["value"] == USER
            assert "@user_id" in body["configuration"]["query"]["query"]
            job_id = body["jobReference"]["jobId"]
            return job_response(job_id, rows=[{"f": [{"v": "0"}]}] if body["configuration"]["query"]["query"].startswith("SELECT") else None, configuration=body["configuration"])
        return job_response(request.url.path.rsplit("/", 1)[-1])
    result = await provider(handler).delete_account_data(user_id=USER, idempotency_key="step")
    assert result["confirmed"] is True
    assert result["readback"] is True
    assert len(calls) == 3
    assert all(call.url.host == "bigquery.googleapis.com" for call in calls)


def test_preview_is_dry_run_and_probe_requires_explicit_scope():
    p = provider(lambda _: httpx.Response(500), tables=(TABLE,))
    body = p.preview_requests()[0]
    assert body["configuration"]["query"]["dryRun"] is True
    assert "DELETE" not in body["configuration"]["query"]["query"]
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_table_scope_invalid"):
        provider(lambda _: httpx.Response(500), tables=("chirps_analytics_probe_20260908",))


def test_snapshot_binding_and_freeze_are_strict_before_transport():
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_freeze_invalid"):
        BigQueryAnalyticsProvider(
            token="x", project=PROJECT, dataset=DATASET, snapshot=snapshot(),
            freeze=AnalyticsFreezeReceipt(USER, "x", time.time() + 10, True, False),
            reviewed_tables=(TABLE,), transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_snapshot_digest_invalid"):
        provider(lambda _: httpx.Response(500), snap=snapshot(digest="wrong"))


@pytest.mark.asyncio
async def test_metadata_requires_exact_table_binding_and_reviewed_schema():
    async def wrong(request):
        return httpx.Response(200, json={"tableReference": {"projectId": "other", "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_table_binding_invalid"):
        await provider(wrong).capture_snapshot()

    async def unknown_schema(request):
        return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": {"fields": []}})
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_schema_invalid"):
        await provider(unknown_schema).capture_snapshot()


@pytest.mark.asyncio
async def test_streaming_buffer_blocks_snapshot():
    async def handler(request):
        return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema(), "streamingBuffer": {"estimatedRows": "1"}})
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_streaming_buffer_present"):
        await provider(handler).capture_snapshot()


@pytest.mark.asyncio
async def test_lost_insert_response_resolves_same_job_without_new_job_id():
    calls = []
    saved = {}
    async def handler(request):
        calls.append(request)
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            saved["configuration"] = json.loads(request.content)["configuration"]
            return httpx.Response(409, json={"error": {"message": "already exists"}})
        is_readback = saved["configuration"]["query"]["query"].startswith("SELECT")
        return job_response(
            request.url.path.rsplit("/", 1)[-1], configuration=saved["configuration"],
            rows=[{"f": [{"v": "0"}]}] if is_readback else None,
        )
    result = await provider(handler).delete_account_data(user_id=USER, idempotency_key="step")
    assert result["affected_rows"] == 1
    assert calls[0].method == "GET"
    assert calls[1].method == "POST"
    assert calls[2].method == "GET"
    assert all("jobId=" not in str(call.url) for call in calls)


@pytest.mark.asyncio
async def test_foreign_job_reference_and_bad_count_fail_closed():
    async def foreign(request):
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={"jobReference": {"projectId": PROJECT, "jobId": "foreign"}, "configuration": body["configuration"], "status": {"state": "DONE"}})
        return httpx.Response(500)
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_job_binding_invalid"):
        await provider(foreign).delete_account_data(user_id=USER, idempotency_key="step")

    async def bad_count(request):
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            body = json.loads(request.content)
            return job_response(body["jobReference"]["jobId"], deleted="not-a-count", configuration=body["configuration"])
        return httpx.Response(500)
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_affected_count_invalid"):
        await provider(bad_count).delete_account_data(user_id=USER, idempotency_key="step")


@pytest.mark.asyncio
async def test_readback_nonzero_redirect_and_oversized_response_fail_closed():
    async def nonzero(request):
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            body = json.loads(request.content)
            query = body["configuration"]["query"]["query"]
            return job_response(body["jobReference"]["jobId"], rows=[{"f": [{"v": "1"}]}] if query.startswith("SELECT") else None, configuration=body["configuration"])
        return httpx.Response(500)
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_readback_nonzero"):
        await provider(nonzero).delete_account_data(user_id=USER, idempotency_key="step")

    async def redirect(request):
        return httpx.Response(302, headers={"location": "https://evil.invalid"})
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_redirect_rejected"):
        await provider(redirect).delete_account_data(user_id=USER, idempotency_key="step")


@pytest.mark.asyncio
async def test_uncertain_job_state_and_subject_are_not_retried():
    calls = 0
    async def pending(request):
        nonlocal calls
        calls += 1
        if "/tables/" in request.url.path:
            return httpx.Response(200, json={"tableReference": {"projectId": PROJECT, "datasetId": DATASET, "tableId": TABLE}, "schema": schema()})
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={"jobReference": {"projectId": PROJECT, "jobId": body["jobReference"]["jobId"]}, "configuration": body["configuration"], "status": {"state": "PENDING"}})
        return httpx.Response(200, json={"jobReference": {"projectId": PROJECT, "jobId": request.url.path.rsplit("/", 1)[-1]}, "status": {"state": "PENDING"}})
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_job_timeout"):
        await provider(pending).delete_account_data(user_id=USER, idempotency_key="step")
    assert calls == 62
    with pytest.raises(AnalyticsProviderUnavailable, match="analytics_subject_mismatch"):
        await provider(lambda _: httpx.Response(500)).delete_account_data(user_id=str(uuid.uuid4()), idempotency_key="step")
