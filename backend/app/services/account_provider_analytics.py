"""Dormant BigQuery adapter; explicit operator freeze, preview and approval required.

No credential discovery, runtime registration, or production calls on import.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, replace
from functools import wraps

import httpx

from app.services.account_fulfillment import ProviderUnavailable

ORIGIN = 'https://bigquery.googleapis.com'
LOCATION = 'US'
MAX_TABLES = 32
MAX_PAGES = 8
MAX_ROWS = 100_000
MAX_RESPONSE_BYTES = 512 * 1024
MAX_SECONDS = 120.0
SNAPSHOT_SECONDS = 900
MIN_BYTES = 10_000_000
MAX_BYTES = 1_000_000_000
POLL_SECONDS = 0.25
PROJECT = re.compile(r'[a-z][a-z0-9-]{4,61}[a-z0-9]\Z')
DATASET = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,1023}\Z')
STDOUT = re.compile(r'run_googleapis_com_stdout_[0-9]{8}\Z')
PROBE = 'chirps_analytics_probe_20260908'
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class AnalyticsProviderUnavailable(ProviderUnavailable):
    pass


def _fail(code: str):
    return AnalyticsProviderUnavailable('analytics_' + code)


def _uuid(value):
    try:
        actual = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise _fail('identity_invalid') from None
    if str(value) != actual:
        raise _fail('identity_invalid')
    return actual


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _integer(value, low, high, code):
    if type(value) is not int or not low <= value <= high:
        raise _fail(code)
    return value


def _number(value, code):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _fail(code)
    return value


def _decimal(value, limit=MAX_BYTES):
    if type(value) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value) or int(value) > limit:
        raise _fail('numeric_response_invalid')
    return int(value)


def _bounded(fn):
    @wraps(fn)
    async def run(*args, **kwargs):
        try:
            async with asyncio.timeout(MAX_SECONDS):
                return await fn(*args, **kwargs)
        except (TimeoutError, httpx.HTTPError):
            raise _fail('transport_unknown') from None
    return run


@dataclass(frozen=True)
class AnalyticsFreezeReceipt:
    user_id: str
    freeze_id: str
    expires_at: float
    verified: bool
    ingestion_drained: bool

    def validate(self, user_id):
        if (self.verified is not True or self.ingestion_drained is not True
                or self.user_id != _uuid(user_id) or type(self.freeze_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.freeze_id)
                or _number(self.expires_at, 'freeze_invalid') <= time.time()):
            raise _fail('freeze_invalid')


@dataclass(frozen=True)
class AnalyticsExclusion:
    name: str
    schema_digest: str
    reviewed_non_account_probe: bool

    def validate(self):
        if (self.name != PROBE or self.reviewed_non_account_probe is not True
                or type(self.schema_digest) is not str or not DIGEST.fullmatch(self.schema_digest)):
            raise _fail('exclusion_invalid')


@dataclass(frozen=True)
class AnalyticsTable:
    name: str
    schema_digest: str
    preview_count: int
    max_bytes_billed: int
    estimated_delete_bytes: int


@dataclass(frozen=True)
class AnalyticsSnapshot:
    request_id: str
    user_id: str
    project: str
    dataset: str
    tables: tuple[AnalyticsTable, ...]
    exclusions: tuple[AnalyticsExclusion, ...]
    freeze_id: str
    created_at: float
    time_travel_hours: int
    snapshot_digest: str = ''

    def payload(self):
        return {key: value for key, value in asdict(self).items() if key != 'snapshot_digest'}

    def validate(self):
        _uuid(self.request_id)
        _uuid(self.user_id)
        if (type(self.project) is not str or not PROJECT.fullmatch(self.project)
                or type(self.dataset) is not str or not DATASET.fullmatch(self.dataset)
                or type(self.tables) is not tuple or not 1 <= len(self.tables) <= MAX_TABLES
                or type(self.exclusions) is not tuple or len(self.exclusions) > 1 or len(self.tables) + len(self.exclusions) > MAX_TABLES):
            raise _fail('snapshot_invalid')
        names = []
        for t in self.tables:
            if (not isinstance(t, AnalyticsTable) or type(t.name) is not str or not STDOUT.fullmatch(t.name)
                    or type(t.schema_digest) is not str or not DIGEST.fullmatch(t.schema_digest)):
                raise _fail('snapshot_invalid')
            names.append(t.name)
            _integer(t.preview_count, 0, MAX_ROWS, 'row_budget_invalid')
            _integer(t.max_bytes_billed, MIN_BYTES, MAX_BYTES, 'byte_budget_invalid')
            _integer(t.estimated_delete_bytes, 0, t.max_bytes_billed, 'byte_budget_invalid')
        if names != sorted(set(names)) or sum(t.preview_count for t in self.tables) > MAX_ROWS:
            raise _fail('row_budget_invalid')
        if sum(t.max_bytes_billed for t in self.tables) > MAX_BYTES:
            raise _fail('byte_budget_invalid')
        for e in self.exclusions:
            if not isinstance(e, AnalyticsExclusion):
                raise _fail('exclusion_invalid')
            e.validate()
        _integer(self.time_travel_hours, 48, 168, 'retention_invalid')
        age = time.time() - _number(self.created_at, 'snapshot_expired')
        if not 0 <= age <= SNAPSHOT_SECONDS:
            raise _fail('snapshot_expired')
        if self.snapshot_digest != _digest(self.payload()):
            raise _fail('snapshot_digest_invalid')


def _fields(schema):
    if not isinstance(schema, dict) or type(schema.get('fields')) is not list:
        raise _fail('schema_invalid')
    out = {}
    for field in schema['fields']:
        if (not isinstance(field, dict) or type(field.get('name')) is not str
                or field['name'] in out):
            raise _fail('schema_invalid')
        out[field['name']] = field
    return out


def _schema(schema, *, probe=False):
    payload = _fields(schema).get('jsonPayload', {})
    if payload.get('type') != 'RECORD' or payload.get('mode', 'NULLABLE') != 'NULLABLE':
        raise _fail('schema_invalid')
    nested = _fields(payload)
    required = {'analytics': 'BOOLEAN', 'probe_id' if probe else 'user_id': 'STRING'}
    if probe and set(nested) != {'schema_version', 'emission_id', 'severity', 'event', 'emitted_at', 'analytics', 'probe_id'}:
        raise _fail('exclusion_schema_invalid')
    for name, typ in required.items():
        f = nested.get(name, {})
        if f.get('type') != typ or f.get('mode', 'NULLABLE') != 'NULLABLE':
            raise _fail('schema_invalid')


class _Rest:
    def __init__(self, token, project, dataset, transport, timeout_seconds):
        if (type(token) is not str or not 1 <= len(token) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise _fail('credentials_invalid')
        if type(project) is not str or not PROJECT.fullmatch(project) or type(dataset) is not str or not DATASET.fullmatch(dataset):
            raise _fail('identifier_invalid')
        if not 1 <= _number(timeout_seconds, 'timeout_invalid') <= 60:
            raise _fail('timeout_invalid')
        self.project, self.dataset = project, dataset
        self._client_options = dict(base_url=ORIGIN, headers={'Authorization': 'Bearer ' + token},
                                    timeout=timeout_seconds, follow_redirects=False, trust_env=False,
                                    transport=transport)
        self.client = httpx.AsyncClient(**self._client_options)
        self.base = f'/bigquery/v2/projects/{project}'
        self.data = f'{self.base}/datasets/{dataset}'

    async def request(self, method, path, **kwargs):
        try:
            async with self.client.stream(method, path, **kwargs) as response:
                if 300 <= response.status_code < 400:
                    raise _fail('redirect_rejected')
                if response.status_code in (404, 409):
                    raise _fail('missing' if response.status_code == 404 else 'conflict')
                if response.status_code != 200:
                    raise _fail('provider_error')
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise _fail('response_too_large')
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError, RecursionError):
                    raise _fail('response_invalid') from None
                if type(payload) is not dict:
                    raise _fail('response_invalid')
                return payload
        except httpx.HTTPError:
            raise _fail('transport_unknown') from None

    async def inventory(self, tables, exclusions):
        dataset = await self.request('GET', self.data)
        ref = dataset.get('datasetReference')
        if (ref != {'projectId': self.project, 'datasetId': self.dataset}
                or dataset.get('location') != LOCATION):
            raise _fail('dataset_binding_invalid')
        hours = _decimal(dataset.get('maxTimeTravelHours'), 168)
        _integer(hours, 48, 168, 'retention_invalid')
        expected = set(tables) | {e.name for e in exclusions}
        names, tokens, page = set(), set(), None
        for _ in range(MAX_PAGES):
            params = {'maxResults': MAX_TABLES}
            if page is not None:
                params['pageToken'] = page
            listing = await self.request('GET', self.data + '/tables', params=params)
            rows = listing.get('tables', [])
            if type(rows) is not list:
                raise _fail('inventory_invalid')
            for row in rows:
                ref = row.get('tableReference') if isinstance(row, dict) else None
                if (not isinstance(ref, dict) or row.get('type') != 'TABLE'
                        or ref.get('projectId') != self.project or ref.get('datasetId') != self.dataset):
                    raise _fail('inventory_invalid')
                name = ref.get('tableId')
                if type(name) is not str or name not in expected or name in names:
                    raise _fail('inventory_drift')
                names.add(name)
                if len(names) > MAX_TABLES:
                    raise _fail('inventory_invalid')
            page = listing.get('nextPageToken')
            if page is None:
                break
            if type(page) is not str or not 1 <= len(page) <= 2048 or page in tokens:
                raise _fail('inventory_invalid')
            tokens.add(page)
        else:
            raise _fail('inventory_page_limit')
        if names != expected:
            raise _fail('inventory_drift')
        schemas = {}
        excluded = {e.name: e for e in exclusions}
        for name in sorted(names):
            body = await self.request('GET', self.data + '/tables/' + name)
            if (body.get('type') != 'TABLE' or body.get('tableReference') != {
                    'projectId': self.project, 'datasetId': self.dataset, 'tableId': name}):
                raise _fail('table_binding_invalid')
            if 'streamingBuffer' in body:
                raise _fail('streaming_buffer_present')
            _schema(body.get('schema'), probe=name in excluded)
            schemas[name] = _digest(body['schema'])
            if name in excluded and schemas[name] != excluded[name].schema_digest:
                raise _fail('exclusion_schema_drift')
        return schemas, hours

    def query(self, table, subject, *, count=None):
        if not STDOUT.fullmatch(table):
            raise _fail('table_invalid')
        relation = f'`{self.project}.{self.dataset}.{table}`'
        predicate = 'jsonPayload.user_id = @user_id AND jsonPayload.analytics IS TRUE'
        params = [{'name': 'user_id', 'parameterType': {'type': 'STRING'}, 'parameterValue': {'value': _uuid(subject)}}]
        if count is None:
            sql = f'SELECT COUNT(*) AS remaining FROM {relation} WHERE {predicate}'
        else:
            _integer(count, 0, MAX_ROWS, 'row_budget_invalid')
            sql = f'DELETE FROM {relation} WHERE {predicate} AND (SELECT COUNT(*) FROM {relation} WHERE {predicate}) = @reviewed_count'
            params.append({'name': 'reviewed_count', 'parameterType': {'type': 'INT64'}, 'parameterValue': {'value': str(count)}})
        return {'query': sql, 'useLegacySql': False, 'useQueryCache': False,
                'parameterMode': 'NAMED', 'queryParameters': params}

    def body(self, job_id, query, budget, *, plan=None, dry=False):
        _integer(budget, MIN_BYTES, MAX_BYTES, 'byte_budget_invalid')
        config = {'dryRun': dry, 'query': {**query, 'maximumBytesBilled': str(budget)}}
        if plan is not None:
            config['labels'] = {'chirp_plan': plan[:63]}
        return {'jobReference': {'projectId': self.project, 'location': LOCATION, 'jobId': job_id}, 'configuration': config}

    def validate_job(self, job, body, *, dry=False):
        if (job.get('jobReference') != body['jobReference']
                and not (dry and job.get('jobReference') is None)):
            raise _fail('job_binding_invalid')
        observed = job.get('configuration')
        expected = body['configuration']
        if not isinstance(observed, dict) or observed.get('dryRun', False) is not dry:
            raise _fail('job_config_mismatch')
        if set(observed) - {'query', 'dryRun', 'jobType', 'labels'} or observed.get('jobType', 'QUERY') != 'QUERY':
            raise _fail('job_config_mismatch')
        if observed.get('labels', {}) != expected.get('labels', {}):
            raise _fail('job_config_mismatch')
        query = observed.get('query')
        if not isinstance(query, dict) or any(query.get(k) != v for k, v in expected['query'].items()):
            raise _fail('job_config_mismatch')
        extras = set(query) - set(expected['query'])
        defaults = {'priority': 'INTERACTIVE', 'createDisposition': 'CREATE_IF_NEEDED',
                    'writeDisposition': 'WRITE_EMPTY', 'allowLargeResults': False}
        for k in extras - {'destinationTable'}:
            # flattenResults is ignored for GoogleSQL. Anonymous result tables
            # may also carry WRITE_TRUNCATE; only allow that with the narrowly
            # validated server-generated SELECT destination below.
            if k == 'flattenResults' and type(query[k]) is bool:
                continue
            if k == 'writeDisposition' and query[k] == 'WRITE_TRUNCATE' and 'destinationTable' in extras:
                continue
            if k not in defaults or query[k] != defaults[k]:
                raise _fail('job_config_mismatch')
        if 'destinationTable' in extras:
            dest = query['destinationTable']
            if (not expected['query']['query'].startswith('SELECT ') or not isinstance(dest, dict)
                    or set(dest) != {'projectId', 'datasetId', 'tableId'} or dest.get('projectId') != self.project
                    or not re.fullmatch(r'_[0-9a-f]{16,128}', str(dest.get('datasetId')))
                    or not re.fullmatch(r'anon[0-9a-f_]{16,128}', str(dest.get('tableId')))):
                raise _fail('job_config_mismatch')
        status = job.get('status')
        if not isinstance(status, dict) or status.get('state') not in ('DONE', 'PENDING', 'RUNNING'):
            raise _fail('job_state_invalid')
        if status.get('errorResult') or status.get('errors'):
            raise _fail('job_failed')
        return status['state']

    async def job(self, body, *, dry=False):
        job_id = body['jobReference']['jobId']
        try:
            job = await self.request('POST', self.base + '/jobs', json=body)
        except AnalyticsProviderUnavailable as exc:
            if dry or str(exc) not in ('analytics_conflict', 'analytics_transport_unknown'):
                raise
            # Resolve exactly the same job, including an uncertain insert response.
            job = await self.request('GET', self.base + '/jobs/' + job_id, params={'location': LOCATION})
        if dry:
            # jobs.insert dry runs may omit the normal Job envelope. The
            # directly returned processing estimate belongs to this POST;
            # validate optional echoed fields, never poll a dry run.
            if 'jobReference' in job and job['jobReference'] != body['jobReference']:
                raise _fail('job_binding_invalid')
            if 'status' in job:
                status = job['status']
                if not isinstance(status, dict) or status.get('errorResult') or status.get('errors'):
                    raise _fail('job_failed')
            if 'configuration' in job:
                self.validate_job({**job, 'status': {'state': 'DONE'}}, body, dry=True)
            return job
        for _ in range(120):
            state = self.validate_job(job, body, dry=dry)
            if state == 'DONE':
                return job
            if dry:
                raise _fail('dry_run_invalid')
            await asyncio.sleep(POLL_SECONDS)
            job = await self.request('GET', self.base + '/jobs/' + job_id, params={'location': LOCATION})
        raise _fail('job_timeout')

    async def count(self, body):
        await self.job(body)
        ref = body['jobReference']
        result = await self.request('GET', self.base + '/queries/' + ref['jobId'], params={'location': LOCATION, 'maxResults': 1})
        if (result.get('jobComplete') is not True or result.get('jobReference') != ref
                or result.get('totalRows') != '1' or result.get('pageToken') or result.get('errors')):
            raise _fail('query_result_invalid')
        fields = _fields(result.get('schema'))
        if set(fields) != {'remaining'} or fields['remaining'].get('type') != 'INTEGER' or fields['remaining'].get('mode', 'NULLABLE') not in ('NULLABLE', 'REQUIRED'):
            raise _fail('query_result_invalid')
        rows = result.get('rows')
        if type(rows) is not list or len(rows) != 1 or not isinstance(rows[0], dict):
            raise _fail('query_result_invalid')
        cells = rows[0].get('f')
        if type(cells) is not list or len(cells) != 1 or not isinstance(cells[0], dict):
            raise _fail('query_result_invalid')
        return _decimal(cells[0].get('v'), MAX_ROWS)


def _job_id(request, operation, table=''):
    # Snapshot deliberately excluded: conflicting plans reuse and reject the same job.
    return 'chirp_' + hashlib.sha256(f'{request}:{operation}:{table}'.encode()).hexdigest()[:48]


class BigQueryAnalyticsProvider:
    def __init__(self, *, token, project, dataset, snapshot, freeze, reviewed_snapshot_digest,
                 transport=None, timeout_seconds=20.0):
        snapshot.validate()
        freeze.validate(snapshot.user_id)
        if (snapshot.project != project or snapshot.dataset != dataset or snapshot.freeze_id != freeze.freeze_id
                or reviewed_snapshot_digest != snapshot.snapshot_digest):
            raise _fail('snapshot_binding_invalid')
        self.snapshot, self.freeze = snapshot, freeze
        self.rest = _Rest(token, project, dataset, transport, timeout_seconds)

    @classmethod
    @_bounded
    async def capture_preview_snapshot(cls, *, token, project, dataset, request_id, user_id, freeze,
                                       reviewed_tables, exclusions=(), query_byte_budget=50_000_000,
                                       transport=None, timeout_seconds=20.0):
        request_id, user_id = _uuid(request_id), _uuid(user_id)
        freeze.validate(user_id)
        _integer(query_byte_budget, MIN_BYTES, MAX_BYTES, 'byte_budget_invalid')
        if (type(reviewed_tables) is not tuple or not 1 <= len(reviewed_tables) <= MAX_TABLES
                or any(type(t) is not str or not STDOUT.fullmatch(t) for t in reviewed_tables)
                or list(reviewed_tables) != sorted(set(reviewed_tables)) or type(exclusions) is not tuple
                or len(exclusions) > 1 or len(reviewed_tables) + len(exclusions) > MAX_TABLES
                or len(reviewed_tables) * query_byte_budget > MAX_BYTES):
            raise _fail('table_scope_invalid')
        for e in exclusions:
            if not isinstance(e, AnalyticsExclusion):
                raise _fail('exclusion_invalid')
            e.validate()
        rest = _Rest(token, project, dataset, transport, timeout_seconds)
        async with rest.client:
            schemas, hours = await rest.inventory(reviewed_tables, exclusions)
            tables = []
            for name in reviewed_tables:
                freeze.validate(user_id)
                count = await rest.count(rest.body('chirp_preview_' + uuid.uuid4().hex,
                                                  rest.query(name, user_id), query_byte_budget))
                if sum(t.preview_count for t in tables) + count > MAX_ROWS:
                    raise _fail('row_budget_exceeded')
                estimate = await rest.job(rest.body('chirp_estimate_' + uuid.uuid4().hex,
                                                    rest.query(name, user_id, count=count), query_byte_budget, dry=True), dry=True)
                stats = estimate.get('statistics')
                query_stats = stats.get('query') if isinstance(stats, dict) else None
                if (not isinstance(query_stats, dict)
                        or query_stats.get('totalBytesProcessedAccuracy') not in ('PRECISE', 'UPPER_BOUND')):
                    raise _fail('estimate_accuracy_unknown')
                processed = _decimal(query_stats.get('totalBytesProcessed'))
                if processed > query_byte_budget:
                    raise _fail('byte_budget_exceeded')
                tables.append(AnalyticsTable(name, schemas[name], count, query_byte_budget, processed))
            if await rest.inventory(reviewed_tables, exclusions) != (schemas, hours):
                raise _fail('snapshot_drift')
            freeze.validate(user_id)
        snapshot = AnalyticsSnapshot(request_id, user_id, project, dataset, tuple(tables), exclusions,
                                     freeze.freeze_id, time.time(), hours)
        snapshot = replace(snapshot, snapshot_digest=_digest(snapshot.payload()))
        snapshot.validate()
        return snapshot

    async def _current(self):
        s = self.snapshot
        schemas, hours = await self.rest.inventory(tuple(t.name for t in s.tables), s.exclusions)
        expected = {t.name: t.schema_digest for t in s.tables} | {e.name: e.schema_digest for e in s.exclusions}
        if schemas != expected or hours != s.time_travel_hours:
            raise _fail('snapshot_drift')

    @_bounded
    async def delete_account_data(self, *, user_id, idempotency_key):
        s, rest = self.snapshot, self.rest
        if _uuid(user_id) != s.user_id or idempotency_key != 'c436:' + s.request_id:
            raise _fail('subject_or_request_mismatch')
        s.validate()
        self.freeze.validate(s.user_id)
        if rest.client.is_closed:
            rest.client = httpx.AsyncClient(**rest._client_options)
        async with rest.client:
            await self._current()
            first = s.tables[0]
            # A stable read-only job locks this request to one plan before any DML.
            # Its rows are never used as fresh evidence on retries.
            await rest.job(rest.body(_job_id(s.request_id, 'plan'), rest.query(first.name, s.user_id),
                                     first.max_bytes_billed, plan=s.snapshot_digest))
            affected = 0
            for t in s.tables:
                s.validate()
                self.freeze.validate(s.user_id)
                job = await rest.job(rest.body(_job_id(s.request_id, 'delete', t.name),
                                               rest.query(t.name, s.user_id, count=t.preview_count),
                                               t.max_bytes_billed, plan=s.snapshot_digest))
                stats = job.get('statistics', {}).get('query', {}) if isinstance(job.get('statistics'), dict) else {}
                deleted = _decimal(stats.get('numDmlAffectedRows') if isinstance(stats, dict) else None, MAX_ROWS)
                if deleted != t.preview_count:
                    raise _fail('reviewed_count_changed')
                affected += deleted
            # Reread every table after every delete, not stale cached COUNT jobs.
            for t in s.tables:
                count = await rest.count(rest.body('chirp_verify_' + uuid.uuid4().hex,
                                                  rest.query(t.name, s.user_id), t.max_bytes_billed,
                                                  plan=s.snapshot_digest))
                if count != 0:
                    raise _fail('readback_nonzero')
            await self._current()
            s.validate()
            self.freeze.validate(s.user_id)
        return {'confirmed': True, 'provider': 'analytics', 'subject': s.user_id,
                'scope': [t.name for t in s.tables], 'readback': True,
                'retained_with_reason': ['bigquery_time_travel', 'bigquery_fail_safe'],
                'time_travel_hours': s.time_travel_hours, 'fail_safe_days': 7,
                'excluded_scope': [e.name for e in s.exclusions],
                'affected_rows': affected, 'snapshot_digest': s.snapshot_digest}
