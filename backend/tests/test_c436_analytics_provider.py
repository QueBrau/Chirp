"""Realistic offline BigQuery protocol tests for the dormant analytics adapter."""
from __future__ import annotations
import asyncio
import json
import time
import httpx
import pytest
from app.services import account_provider_analytics as mod
from app.services.account_provider_analytics import AnalyticsExclusion, AnalyticsFreezeReceipt, AnalyticsProviderUnavailable, AnalyticsSnapshot, AnalyticsTable, BigQueryAnalyticsProvider, PROBE, _digest
USER = '12345678-1234-4234-8234-123456789abc'
PROJECT = 'chirps-prod'
DATASET = 'chirp_analytics'
TABLE = 'run_googleapis_com_stdout_20261012'
RID = '11111111-1111-4111-8111-111111111111'
KEY = 'c436:' + RID

def user_schema():
    return {'fields': [{'name': 'jsonPayload', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [{'name': 'analytics', 'type': 'BOOLEAN', 'mode': 'NULLABLE'}, {'name': 'user_id', 'type': 'STRING', 'mode': 'NULLABLE'}]}]}

def probe_schema():
    return {'fields': [{'name': 'jsonPayload', 'type': 'RECORD', 'mode': 'NULLABLE', 'fields': [{'name': x, 'type': t, 'mode': 'NULLABLE'} for x, t in [('schema_version', 'STRING'), ('emission_id', 'STRING'), ('severity', 'STRING'), ('event', 'STRING'), ('emitted_at', 'TIMESTAMP'), ('analytics', 'BOOLEAN'), ('probe_id', 'STRING')]]}]}

def fr():
    return AnalyticsFreezeReceipt(USER, 'freeze', time.time() + 600, True, True)

def exclusion():
    return AnalyticsExclusion(PROBE, _digest(probe_schema()), True)

def meta(name=TABLE, schema=None):
    return {'type': 'TABLE', 'tableReference': {'projectId': PROJECT, 'datasetId': DATASET, 'tableId': name}, 'schema': schema or user_schema()}

def capture_handler(request, *, count=2):
    p = request.url.path
    if p.endswith('/datasets/' + DATASET):
        return httpx.Response(200, json={'datasetReference': {'projectId': PROJECT, 'datasetId': DATASET}, 'location': 'US', 'maxTimeTravelHours': '168'})
    if p.endswith('/tables'):
        rows = [meta()]
        return httpx.Response(200, json={'tables': rows, 'nextPageToken': None})
    if '/tables/' + TABLE in p:
        return httpx.Response(200, json=meta())
    if p.endswith('/jobs'):
        b = json.loads(request.content)
        q = b['configuration']['query']['query']
        if b['configuration'].get('dryRun'):
            return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'DONE'}, 'statistics': {'totalBytesProcessed': '10000000', 'query': {'totalBytesProcessed': '10000000', 'totalBytesProcessedAccuracy': 'PRECISE'}}})
        return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'DONE'}, 'statistics': {'query': {'totalBytesProcessed': '10000000', 'numDmlAffectedRows': str(count)}}})
    if '/queries/' in p:
        jid = p.rsplit('/', 1)[-1]
        value = 0 if jid.startswith('chirp_verify_') else count
        return httpx.Response(200, json={'jobReference': {'projectId': PROJECT, 'location': 'US', 'jobId': jid}, 'jobComplete': True, 'totalRows': '1', 'schema': {'fields': [{'name': 'remaining', 'type': 'INTEGER', 'mode': 'NULLABLE'}]}, 'rows': [{'f': [{'v': str(value)}]}]})
    return httpx.Response(500)

def snap(count=2, bytes_=50000000, schema_digest=None, exclusion=()):
    t = AnalyticsTable(TABLE, schema_digest or _digest(user_schema()), count, bytes_, 10000000)
    s = AnalyticsSnapshot(RID, USER, PROJECT, DATASET, (t,), tuple(exclusion), 'freeze', time.time(), 168)
    return AnalyticsSnapshot(**{**s.__dict__, 'snapshot_digest': _digest(s.payload())})

def make_provider(handler, s=None):
    actual = s or snap()
    return BigQueryAnalyticsProvider(token='tok', project=PROJECT, dataset=DATASET, snapshot=actual, freeze=fr(), reviewed_snapshot_digest=actual.snapshot_digest, transport=httpx.MockTransport(handler), timeout_seconds=2)

@pytest.mark.asyncio
async def test_capture_inventory_preview_dryrun_and_query_envelope():

    async def h(req):
        return capture_handler(req)
    s = await BigQueryAnalyticsProvider.capture_preview_snapshot(token='tok', project=PROJECT, dataset=DATASET, request_id=RID, user_id=USER, freeze=fr(), reviewed_tables=(TABLE,), transport=httpx.MockTransport(h), timeout_seconds=2)
    assert s.tables[0].preview_count == 2 and s.tables[0].max_bytes_billed == 50000000

@pytest.mark.asyncio
async def test_apply_uses_count_guard_bytes_and_fresh_readback():
    calls = []

    async def h(req):
        calls.append(req)
        return capture_handler(req, count=2)
    result = await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)
    posts = [json.loads(r.content) for r in calls if r.method == 'POST']
    assert result['confirmed'] and result['retained_with_reason'] == ['bigquery_time_travel', 'bigquery_fail_safe']
    assert any(('= @reviewed_count' in p['configuration']['query']['query'] for p in posts))
    assert all((p['configuration']['query']['maximumBytesBilled'] == '50000000' for p in posts))

@pytest.mark.asyncio
async def test_inventory_added_table_blocks_before_delete():

    async def h(req):
        if req.url.path.endswith('/tables'):
            return httpx.Response(200, json={'tables': [meta(), meta('run_googleapis_com_stdout_20261013')]})
        return capture_handler(req)
    with pytest.raises(AnalyticsProviderUnavailable, match='inventory_drift'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)

@pytest.mark.asyncio
async def test_streaming_buffer_presence_blocks_even_empty():

    async def h(req):
        if '/tables/' + TABLE in req.url.path:
            x = meta()
            x['streamingBuffer'] = {}
            return httpx.Response(200, json=x)
        return capture_handler(req)
    with pytest.raises(AnalyticsProviderUnavailable, match='streaming_buffer_present'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)

@pytest.mark.asyncio
async def test_foreign_poll_reference_and_malformed_query_rows_fail():
    calls = []

    async def h(req):
        calls.append(req)
        if req.url.path.endswith('/jobs'):
            b = json.loads(req.content)
            return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'PENDING'}})
        if '/jobs/' in req.url.path and '/queries/' not in req.url.path:
            jid = req.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={'jobReference': {'projectId': 'foreign', 'location': 'US', 'jobId': jid}, 'configuration': {'dryRun': False}, 'status': {'state': 'DONE'}})
        return capture_handler(req)
    with pytest.raises(AnalyticsProviderUnavailable, match='job_binding_invalid'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert any((r.method == 'GET' and '/jobs/' in r.url.path for r in calls))

@pytest.mark.asyncio
async def test_changed_delete_count_never_confirms():

    async def h(req):
        return capture_handler(req, count=1)
    with pytest.raises(AnalyticsProviderUnavailable, match='reviewed_count_changed'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)

@pytest.mark.asyncio
async def test_query_result_malformed_cells_fail_closed():

    async def h(req):
        if '/queries/' in req.url.path:
            jid = req.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={'jobReference': {'projectId': PROJECT, 'location': 'US', 'jobId': jid}, 'jobComplete': True, 'totalRows': '1', 'schema': {'fields': [{'name': 'remaining', 'type': 'INTEGER'}]}, 'rows': [{'f': []}]})
        return capture_handler(req)
    with pytest.raises(AnalyticsProviderUnavailable, match='query_result_invalid'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)

@pytest.mark.asyncio
async def test_poll_location_mismatch_fails_closed():
    calls = []

    async def h(req):
        calls.append(req)
        if req.url.path.endswith('/jobs'):
            b = json.loads(req.content)
            return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'PENDING'}})
        if '/jobs/' in req.url.path and '/queries/' not in req.url.path:
            jid = req.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={'jobReference': {'projectId': PROJECT, 'location': 'EU', 'jobId': jid}, 'configuration': {'dryRun': False}, 'status': {'state': 'DONE'}})
        return capture_handler(req)
    with pytest.raises(AnalyticsProviderUnavailable, match='job_binding_invalid'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert any((r.method == 'GET' and '/jobs/' in r.url.path for r in calls))

@pytest.mark.asyncio
async def test_redirect_is_rejected_without_following():

    async def h(req):
        return httpx.Response(302, headers={'location': 'https://evil.invalid'})
    with pytest.raises(AnalyticsProviderUnavailable, match='redirect_rejected'):
        await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)

@pytest.mark.asyncio
async def test_lost_post_conflict_resolves_same_stable_plan_and_delete_ids():
    calls = []
    posted = []
    bodies = {}

    async def h(req):
        calls.append(req)
        if req.method == 'POST':
            b = json.loads(req.content)
            posted.append(b['jobReference']['jobId'])
            bodies[b['jobReference']['jobId']] = b
            if len(posted) == 1:
                raise httpx.ReadTimeout('lost response')
            return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'DONE'}, 'statistics': {'query': {'numDmlAffectedRows': '2'}}})
        if '/jobs/' in req.url.path and '/queries/' not in req.url.path:
            jid = req.url.path.rsplit('/', 1)[-1]
            b = bodies[jid]
            return httpx.Response(200, json={'jobReference': b['jobReference'], 'configuration': b['configuration'], 'status': {'state': 'DONE'}, 'statistics': {'query': {'numDmlAffectedRows': '2'}}})
        return capture_handler(req)
    result = await make_provider(h).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert result['confirmed']
    assert any((r.method == 'GET' and posted[0] in r.url.path for r in calls))

@pytest.mark.parametrize('bad', [-1, 1.0, True])
def test_snapshot_row_budget_types_rejected(bad):
    t = AnalyticsTable(TABLE, _digest(user_schema()), bad, 50000000, 10000000)
    s = AnalyticsSnapshot(RID, USER, PROJECT, DATASET, (t,), (), 'freeze', time.time(), 168)
    with pytest.raises(AnalyticsProviderUnavailable):
        BigQueryAnalyticsProvider(token='tok', project=PROJECT, dataset=DATASET, snapshot=s, freeze=fr(), reviewed_snapshot_digest='x', transport=httpx.MockTransport(lambda r: httpx.Response(500)))

@pytest.mark.asyncio
async def test_subject_credentials_and_request_binding_fail_closed():
    actual = snap()
    with pytest.raises(AnalyticsProviderUnavailable):
        await make_provider(lambda r: httpx.Response(500)).delete_account_data(user_id=USER, idempotency_key='other:' + RID)
    with pytest.raises(AnalyticsProviderUnavailable, match='credentials_invalid'):
        BigQueryAnalyticsProvider(token='bad\n', project=PROJECT, dataset=DATASET, snapshot=actual, freeze=fr(), reviewed_snapshot_digest=actual.snapshot_digest, transport=httpx.MockTransport(lambda r: httpx.Response(500)))

class FakeBigQuery:
    """Stateful REST fixture: jobs persist and only guarded DML changes rows."""

    def __init__(self, rows=2):
        self.rows, self.jobs, self.requests, self.executed_deletes = (rows, {}, [], 0)
        self.probe = self.extra_table = self.probe_drift = False

    def __call__(self, request):
        import copy
        self.requests.append(request)
        path = request.url.path
        if path.endswith('/tables'):
            values = [meta()]
            if self.extra_table:
                values.append(meta('run_googleapis_com_stdout_20261013'))
            if self.probe:
                if request.url.params.get('pageToken') == 'page&two':
                    return httpx.Response(200, json={'tables': [meta(PROBE, probe_schema())]})
                return httpx.Response(200, json={'tables': values, 'nextPageToken': 'page&two'})
            return httpx.Response(200, json={'tables': values})
        if path.endswith('/tables/' + PROBE):
            schema = probe_schema()
            if self.probe_drift:
                schema['fields'].append({'name': 'unexpected', 'type': 'STRING'})
            return httpx.Response(200, json=meta(PROBE, schema))
        if path.endswith('/jobs'):
            body = json.loads(request.content)
            config = body['configuration']
            if config.get('dryRun'):
                return httpx.Response(200, json={'statistics': {'query': {'totalBytesProcessed': '10000000', 'totalBytesProcessedAccuracy': 'PRECISE'}}})
            job_id = body['jobReference']['jobId']
            if job_id in self.jobs:
                return httpx.Response(409, json={'error': {'message': 'job already exists'}})
            value = copy.deepcopy(body)
            value['configuration'].pop('dryRun')
            value['configuration']['jobType'] = 'QUERY'
            value['configuration']['query'].update(priority='INTERACTIVE', writeDisposition='WRITE_EMPTY', flattenResults=False)
            value['status'] = {'state': 'DONE'}
            if config['query']['query'].startswith('DELETE '):
                reviewed = int(next((p for p in config['query']['queryParameters'] if p['name'] == 'reviewed_count'))['parameterValue']['value'])
                affected = self.rows if self.rows == reviewed else 0
                self.rows -= affected
                self.executed_deletes += 1
                value['statistics'] = {'query': {'numDmlAffectedRows': str(affected)}}
            self.jobs[job_id] = value
            return httpx.Response(200, json=value)
        if '/jobs/' in path:
            return httpx.Response(200, json=self.jobs[path.rsplit('/', 1)[-1]])
        if '/queries/' in path:
            body = capture_handler(request).json()
            body['rows'] = [{'f': [{'v': str(self.rows)}]}]
            return httpx.Response(200, json=body)
        return capture_handler(request)

    def verify_ids(self):
        return [json.loads(r.content)['jobReference']['jobId'] for r in self.requests if r.method == 'POST' and json.loads(r.content)['jobReference']['jobId'].startswith('chirp_verify_')]

async def capture(service, exclusions=()):
    return await BigQueryAnalyticsProvider.capture_preview_snapshot(token='tok', project=PROJECT, dataset=DATASET, request_id=RID, user_id=USER, freeze=fr(), reviewed_tables=(TABLE,), exclusions=exclusions, transport=httpx.MockTransport(service), timeout_seconds=2)

@pytest.mark.asyncio
async def test_full_capture_apply_retry_has_one_delete_and_fresh_zero_queries():
    service = FakeBigQuery()
    snapshot = await capture(service)
    assert snapshot.tables[0].preview_count == 2
    assert snapshot.tables[0].estimated_delete_bytes == 10000000
    provider = make_provider(service, snapshot)
    first = await provider.delete_account_data(user_id=USER, idempotency_key=KEY)
    second = await provider.delete_account_data(user_id=USER, idempotency_key=KEY)
    assert first == second
    assert service.rows == 0 and service.executed_deletes == 1
    assert len(service.verify_ids()) == len(set(service.verify_ids())) == 2
    assert sum((r.method == 'GET' and '/jobs/' in r.url.path for r in service.requests)) == 2

@pytest.mark.asyncio
async def test_changed_plan_for_same_request_rejected_before_second_delete():
    from dataclasses import replace
    service = FakeBigQuery()
    original = await capture(service)
    await make_provider(service, original).delete_account_data(user_id=USER, idempotency_key=KEY)
    changed = replace(original, tables=(replace(original.tables[0], preview_count=0),))
    changed = replace(changed, snapshot_digest=_digest(changed.payload()))
    with pytest.raises(AnalyticsProviderUnavailable, match='job_config_mismatch'):
        await make_provider(service, changed).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.executed_deletes == 1

@pytest.mark.asyncio
async def test_count_guard_leaves_changed_rows_untouched():
    service = FakeBigQuery()
    snapshot = await capture(service)
    service.rows = 3
    with pytest.raises(AnalyticsProviderUnavailable, match='reviewed_count_changed'):
        await make_provider(service, snapshot).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.rows == 3 and (not service.verify_ids())

@pytest.mark.asyncio
async def test_complete_paginated_inventory_and_reviewed_probe_are_checked():
    service = FakeBigQuery()
    service.probe = True
    snapshot = await capture(service, (exclusion(),))
    result = await make_provider(service, snapshot).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert result['excluded_scope'] == [PROBE]
    assert service.executed_deletes == 1
    assert sum((r.url.params.get('pageToken') == 'page&two' for r in service.requests)) == 4
    assert sum((r.url.path.endswith('/tables/' + PROBE) for r in service.requests)) == 4
    for r in service.requests:
        if r.method == 'POST':
            assert PROBE not in json.loads(r.content)['configuration']['query']['query']

@pytest.mark.asyncio
async def test_probe_schema_drift_blocks_before_dml():
    service = FakeBigQuery()
    service.probe = True
    snapshot = await capture(service, (exclusion(),))
    service.probe_drift = True
    with pytest.raises(AnalyticsProviderUnavailable, match='exclusion_schema_drift'):
        await make_provider(service, snapshot).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.executed_deletes == 0

@pytest.mark.asyncio
async def test_table_added_after_delete_prevents_false_completion():
    service = FakeBigQuery()
    snapshot = await capture(service)

    def handler(request):
        response = service(request)
        if '/queries/' in request.url.path:
            service.extra_table = True
        return response
    with pytest.raises(AnalyticsProviderUnavailable, match='inventory_drift'):
        await make_provider(handler, snapshot).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.rows == 0 and service.executed_deletes == 1

@pytest.mark.parametrize('bad', ['UNKNOWN', 'LOWER_BOUND', None])
@pytest.mark.asyncio
async def test_uncertain_dry_run_estimate_never_produces_snapshot(bad):
    service = FakeBigQuery()

    def handler(request):
        response = service(request)
        if request.method == 'POST' and json.loads(request.content)['configuration'].get('dryRun'):
            value = response.json()
            value['statistics']['query']['totalBytesProcessedAccuracy'] = bad
            return httpx.Response(200, json=value)
        return response
    with pytest.raises(AnalyticsProviderUnavailable, match='estimate_accuracy_unknown'):
        await capture(handler)
    assert service.executed_deletes == 0

@pytest.mark.asyncio
async def test_dry_run_over_budget_never_produces_snapshot():
    service = FakeBigQuery()

    def handler(request):
        response = service(request)
        if request.method == 'POST' and json.loads(request.content)['configuration'].get('dryRun'):
            value = response.json()
            value['statistics']['query']['totalBytesProcessed'] = '50000001'
            return httpx.Response(200, json=value)
        return response
    with pytest.raises(AnalyticsProviderUnavailable, match='byte_budget_exceeded'):
        await capture(handler)
    assert service.executed_deletes == 0

@pytest.mark.parametrize('field,value', [('connectionProperties', [{'key': 'session_id', 'value': 'foreign'}]), ('userDefinedFunctionResources', [{'inlineCode': 'bad'}]), ('destinationTable', {'projectId': PROJECT, 'datasetId': DATASET, 'tableId': 'important'})])
@pytest.mark.asyncio
async def test_execution_altering_server_fields_rejected(field, value):
    service = FakeBigQuery()

    def handler(request):
        response = service(request)
        if request.method == 'POST':
            body = response.json()
            body['configuration']['query'][field] = value
            return httpx.Response(200, json=body)
        return response
    with pytest.raises(AnalyticsProviderUnavailable, match='job_config_mismatch'):
        await make_provider(handler).delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.executed_deletes == 0

@pytest.mark.asyncio
async def test_realistic_anonymous_count_result_destination_is_accepted():
    service = FakeBigQuery()

    def handler(request):
        response = service(request)
        if request.method == 'POST':
            body = response.json()
            if body['configuration']['query']['query'].startswith('SELECT '):
                body['configuration']['query'].update(destinationTable={'projectId': PROJECT, 'datasetId': '_' + 'a' * 32, 'tableId': 'anon' + 'b' * 32}, writeDisposition='WRITE_TRUNCATE')
            return httpx.Response(200, json=body)
        return response
    assert (await make_provider(handler).delete_account_data(user_id=USER, idempotency_key=KEY))['confirmed']

@pytest.mark.parametrize('age', [901, -60])
def test_stale_or_future_snapshot_is_rejected_before_http(age):
    from dataclasses import replace
    snapshot = replace(snap(), created_at=time.time() - age)
    snapshot = replace(snapshot, snapshot_digest=_digest(snapshot.payload()))
    with pytest.raises(AnalyticsProviderUnavailable, match='snapshot_expired'):
        make_provider(lambda request: pytest.fail('unexpected HTTP'), snapshot)

class Chunks(httpx.AsyncByteStream):

    def __init__(self, chunks):
        self.chunks = chunks
        self.index = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.index += 1
            yield chunk

    async def aclose(self):
        self.closed = True

@pytest.mark.asyncio
async def test_stream_cap_stops_without_buffering_whole_response():
    stream = Chunks([b'x' * 16384 for _ in range(80)])

    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=stream)
    rest = mod._Rest('tok', PROJECT, DATASET, httpx.MockTransport(handler), 5)
    try:
        with pytest.raises(mod.AnalyticsProviderUnavailable, match='analytics_response_too_large'):
            await rest.request('GET', '/oversize')
        assert stream.index < 80
        assert stream.closed
    finally:
        await rest.client.aclose()

@pytest.mark.asyncio
async def test_capture_overall_deadline_is_sanitized(monkeypatch):
    monkeypatch.setattr(mod, 'MAX_SECONDS', 0.01)

    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})
    with pytest.raises(mod.AnalyticsProviderUnavailable, match='analytics_transport_unknown'):
        await mod.BigQueryAnalyticsProvider.capture_preview_snapshot(token='tok', project=PROJECT, dataset=DATASET, request_id=RID, user_id=USER, freeze=fr(), reviewed_tables=(TABLE,), transport=httpx.MockTransport(slow), timeout_seconds=5)

@pytest.mark.asyncio
async def test_redirect_is_rejected_without_following_or_leaking_host():
    calls = []

    async def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://secret.invalid/private'})
    rest = mod._Rest('tok', PROJECT, DATASET, httpx.MockTransport(redirect), 5)
    try:
        with pytest.raises(mod.AnalyticsProviderUnavailable, match='analytics_redirect_rejected') as exc:
            await rest.request('GET', '/redirect')
        assert len(calls) == 1
        assert 'secret.invalid' not in str(exc.value)
    finally:
        await rest.client.aclose()

def test_error_text_does_not_include_response_body():

    async def no_body(request):
        return httpx.Response(500, text='provider-secret-sentinel')

    async def run():
        rest = mod._Rest('tok', PROJECT, DATASET, httpx.MockTransport(no_body), 5)
        try:
            with pytest.raises(mod.AnalyticsProviderUnavailable) as exc:
                await rest.request('GET', '/error')
            assert str(exc.value) == 'analytics_provider_error'
            assert 'provider-secret-sentinel' not in str(exc.value)
        finally:
            await rest.client.aclose()
    asyncio.run(run())

@pytest.mark.asyncio
async def test_expiring_freeze_after_readback_prevents_confirmation(monkeypatch):
    from types import SimpleNamespace
    now = time.time()
    current = [now]
    service = FakeBigQuery()

    def handler(request):
        response = service(request)
        if '/queries/' in request.url.path:
            current[0] = now + 700
        return response
    provider = make_provider(handler)
    current[0] = time.time()
    monkeypatch.setattr(mod, 'time', SimpleNamespace(time=lambda: current[0]))
    with pytest.raises(AnalyticsProviderUnavailable, match='freeze_invalid'):
        await provider.delete_account_data(user_id=USER, idempotency_key=KEY)
    assert service.executed_deletes == 1

@pytest.mark.asyncio
async def test_apply_overall_deadline_is_sanitized(monkeypatch):
    monkeypatch.setattr(mod, 'MAX_SECONDS', 0.01)

    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})
    with pytest.raises(AnalyticsProviderUnavailable, match='transport_unknown'):
        await make_provider(slow).delete_account_data(user_id=USER, idempotency_key=KEY)
