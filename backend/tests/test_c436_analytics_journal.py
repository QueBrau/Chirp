"""Real PostgreSQL journal wiring test for the injected analytics provider."""
from __future__ import annotations
import json
import time, uuid
import httpx
import pytest
from sqlalchemy import delete, select
from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import PROVIDER_STEPS, run_deletion
from app.services.account_provider_analytics import (
 AnalyticsFreezeReceipt, AnalyticsSnapshot, AnalyticsTable,
 BigQueryAnalyticsProvider, _digest,
)
from tests.test_c436_fulfillment import ConfirmingProvider


def _schema():
 return {'fields':[{'name':'jsonPayload','type':'RECORD','mode':'NULLABLE','fields':[{'name':'analytics','type':'BOOLEAN','mode':'NULLABLE'},{'name':'user_id','type':'STRING','mode':'NULLABLE'}]}]}

@pytest.mark.asyncio
async def test_actual_analytics_provider_is_journaled_with_exact_request_key(client, make_user):
 owner = await make_user('Analytics journal owner')
 created = await client.post('/me/data-requests', json={'kind':'deletion'}, headers=owner.headers)
 assert created.status_code == 201, created.text
 request_id = uuid.UUID(created.json()['id'])
 user_id = uuid.UUID(owner.id)
 table = AnalyticsTable('run_googleapis_com_stdout_20261012', _digest(_schema()), 0, 10_000_000, 0)
 snapshot = AnalyticsSnapshot(request_id=str(request_id), user_id=str(user_id), project='chirps-prod', dataset='chirps_analytics', tables=(table,), exclusions=(), freeze_id='freeze', created_at=time.time(), time_travel_hours=168)
 snapshot = AnalyticsSnapshot(**{**snapshot.__dict__, 'snapshot_digest': _digest(snapshot.payload())})
 async def blocked(request):
  return httpx.Response(500, json={'error': {'message': 'offline test'}})
 analytics = BigQueryAnalyticsProvider(token='offline-token', project='chirps-prod', dataset='chirps_analytics', snapshot=snapshot, freeze=AnalyticsFreezeReceipt(str(user_id), 'freeze', time.time()+600, True, True), reviewed_snapshot_digest=snapshot.snapshot_digest, transport=httpx.MockTransport(blocked), timeout_seconds=1)
 providers = {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}
 providers['analytics'] = analytics
 async with get_session_factory()() as session:
  user = await session.get(models.User, user_id)
  assert user is not None
  result = await run_deletion(session, request_id, user, providers)
  assert result.status == 'blocked' and result.failed_step == 'analytics'
  step = await session.scalar(select(models.AccountFulfillmentStep).where(models.AccountFulfillmentStep.request_id == request_id, models.AccountFulfillmentStep.step_key == 'analytics'))
  assert step is not None and step.idempotency_key == f'account-deletion:{request_id}:analytics'
  assert step.status in {'failed','manual_review'}

@pytest.mark.asyncio
async def test_completed_legacy_request_is_immutable_and_never_retried(client, make_user):
 owner = await make_user('Legacy completed owner')
 created = await client.post('/me/data-requests', json={'kind':'deletion'}, headers=owner.headers)
 assert created.status_code == 201, created.text
 request_id = uuid.UUID(created.json()['id'])
 calls = []
 class NeverProvider(ConfirmingProvider):
  async def delete_account_data(self, *, user, idempotency_key):
   calls.append(idempotency_key)
   raise AssertionError('legacy terminal request invoked provider')
 async with get_session_factory()() as session:
  request = await session.get(models.AccountDataRequest, request_id)
  user = await session.get(models.User, uuid.UUID(owner.id))
  assert request is not None and user is not None
  request.status = 'completed'
  request.failure_code = None
  await session.execute(delete(models.AccountFulfillmentStep).where(models.AccountFulfillmentStep.request_id == request_id, models.AccountFulfillmentStep.step_key == 'analytics'))
  await session.commit()
  before = (request.status, request.provider_steps, request.completed_at)
  providers = {key: NeverProvider(key) for key in PROVIDER_STEPS}
  assert (await run_deletion(session, request_id, user, providers)).status == 'blocked'
  after = await session.get(models.AccountDataRequest, request_id)
  assert after is not None and (after.status, after.provider_steps, after.completed_at) == before
  user = await session.get(models.User, uuid.UUID(owner.id))
  assert user is not None
  assert (await run_deletion(session, request_id, user, providers)).status == 'blocked'
  assert calls == []

@pytest.mark.asyncio
async def test_successful_actual_analytics_receipt_survives_reload(client, make_user):
    owner = await make_user("Analytics successful owner")
    created = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert created.status_code == 201, created.text
    request_id = uuid.UUID(created.json()["id"])
    user_id = uuid.UUID(owner.id)
    schema = _schema()
    table_name = "run_googleapis_com_stdout_20261012"
    table = AnalyticsTable(table_name, _digest(schema), 0, 10_000_000, 0)
    snapshot = AnalyticsSnapshot(str(request_id), str(user_id), "chirps-prod", "chirps_analytics", (table,), (), "freeze", time.time(), 168)
    snapshot = AnalyticsSnapshot(**{**snapshot.__dict__, "snapshot_digest": _digest(snapshot.payload())})

    async def handler(request):
        path = request.url.path
        reference = {"projectId": "chirps-prod", "datasetId": "chirps_analytics", "tableId": table_name}
        if path.endswith("/datasets/chirps_analytics"):
            return httpx.Response(200, json={"datasetReference": {"projectId": "chirps-prod", "datasetId": "chirps_analytics"}, "location": "US", "maxTimeTravelHours": "168"})
        if path.endswith("/tables"):
            return httpx.Response(200, json={"tables": [{"type": "TABLE", "tableReference": reference}]})
        if path.endswith("/tables/" + table_name):
            return httpx.Response(200, json={"type": "TABLE", "tableReference": reference, "schema": schema})
        if path.endswith("/jobs"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"jobReference": body["jobReference"], "configuration": body["configuration"], "status": {"state": "DONE"}, "statistics": {"query": {"numDmlAffectedRows": "0", "totalBytesProcessed": "10000000", "totalBytesProcessedAccuracy": "PRECISE"}}})
        if "/queries/" in path:
            job_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"jobReference": {"projectId": "chirps-prod", "location": "US", "jobId": job_id}, "jobComplete": True, "totalRows": "1", "schema": {"fields": [{"name": "remaining", "type": "INTEGER"}]}, "rows": [{"f": [{"v": "0"}]}]})
        return httpx.Response(500)

    analytics = BigQueryAnalyticsProvider(token="offline-token", project="chirps-prod", dataset="chirps_analytics", snapshot=snapshot, freeze=AnalyticsFreezeReceipt(str(user_id), "freeze", time.time() + 600, True, True), reviewed_snapshot_digest=snapshot.snapshot_digest, transport=httpx.MockTransport(handler), timeout_seconds=1)
    providers = {key: ConfirmingProvider(key) for key in PROVIDER_STEPS}
    providers["analytics"] = analytics
    async with get_session_factory()() as session:
        user = await session.get(models.User, user_id)
        assert user is not None
        result = await run_deletion(session, request_id, user, providers)
        failed = await session.scalar(select(models.AccountFulfillmentStep).where(models.AccountFulfillmentStep.request_id == request_id, models.AccountFulfillmentStep.step_key == "analytics"))
        assert result.status == "completed", (result, failed.status if failed else None, failed.last_error if failed else None)
        step = await session.scalar(select(models.AccountFulfillmentStep).where(models.AccountFulfillmentStep.request_id == request_id, models.AccountFulfillmentStep.step_key == "analytics"))
        assert step is not None and step.provider_ref["request_id"] == str(request_id)
        assert step.provider_ref["snapshot_digest"] == snapshot.snapshot_digest
        assert step.provider_ref["retained_with_reason"] == ["bigquery_time_travel", "bigquery_fail_safe"]
        assert (await run_deletion(session, request_id, user, {key: ConfirmingProvider(key) for key in PROVIDER_STEPS})).status == "completed"
