"""Dormant, operator-supplied BigQuery analytics deletion provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.services.account_fulfillment import ProviderUnavailable

ORIGIN = "https://bigquery.googleapis.com"
LOCATION = "US"
MAX_TABLES = 6
MAX_ROWS = 100_000
MAX_RESPONSE_BYTES = 512 * 1024
MAX_POLLS = 60
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_]{1,256}$")
USER_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class AnalyticsProviderUnavailable(ProviderUnavailable):
    pass


def _fail(code: str) -> AnalyticsProviderUnavailable:
    return AnalyticsProviderUnavailable(code)


def _canonical_uuid(value: uuid.UUID | str) -> str:
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise _fail("analytics_subject_invalid") from None
    if not USER_RE.fullmatch(result):
        raise _fail("analytics_subject_invalid")
    return result


def _future(value: float) -> None:
    if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= time.time():
        raise _fail("analytics_freeze_invalid")


@dataclass(frozen=True)
class AnalyticsFreezeReceipt:
    user_id: str
    freeze_id: str
    expires_at: float
    verified: bool
    ingestion_drained: bool

    def validate(self, user_id: str) -> None:
        if (
            self.verified is not True or self.ingestion_drained is not True
            or self.user_id != user_id or not isinstance(self.freeze_id, str)
            or not 1 <= len(self.freeze_id) <= 128
        ):
            raise _fail("analytics_freeze_invalid")
        if _canonical_uuid(user_id) != user_id or _canonical_uuid(self.user_id) != self.user_id:
            raise _fail("analytics_freeze_invalid")
        _future(self.expires_at)


@dataclass(frozen=True)
class AnalyticsTable:
    name: str
    schema_digest: str
    streaming_buffer: bool


@dataclass(frozen=True)
class AnalyticsSnapshot:
    request_id: str
    user_id: str
    project: str
    dataset: str
    tables: tuple[AnalyticsTable, ...]
    freeze_id: str
    snapshot_digest: str
    created_at: str

    def payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id, "user_id": self.user_id,
            "project": self.project, "dataset": self.dataset,
            "tables": [{"name": t.name, "schema_digest": t.schema_digest, "streaming_buffer": t.streaming_buffer} for t in self.tables],
            "freeze_id": self.freeze_id, "created_at": self.created_at,
        }


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fields(schema: object) -> dict[str, dict[str, object]]:
    if not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
        raise _fail("analytics_schema_invalid")
    result: dict[str, dict[str, object]] = {}
    for field in schema["fields"]:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] in result:
            raise _fail("analytics_schema_invalid")
        result[field["name"]] = field
    return result


def _validate_schema(schema: object) -> None:
    payload = _fields(schema).get("jsonPayload")
    if not isinstance(payload, dict) or payload.get("type") != "RECORD":
        raise _fail("analytics_schema_invalid")
    nested = _fields(payload)
    if nested.get("user_id", {}).get("type") != "STRING" or nested.get("analytics", {}).get("type") != "BOOLEAN":
        raise _fail("analytics_schema_invalid")


def _relation(project: str, dataset: str, table: str) -> str:
    if not IDENTIFIER.fullmatch(project) or not IDENTIFIER.fullmatch(dataset) or not TABLE_RE.fullmatch(table):
        raise _fail("analytics_identifier_invalid")
    tick = chr(96)
    return f"{tick}{project}.{dataset}.{table}{tick}"


class BigQueryAnalyticsProvider:
    """Operator-only provider requiring an explicit freeze and immutable snapshot."""

    def __init__(
        self, *, token: str, project: str, dataset: str, snapshot: AnalyticsSnapshot,
        freeze: AnalyticsFreezeReceipt, reviewed_tables: tuple[str, ...],
        transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0,
    ) -> None:
        if type(token) is not str or not token or len(token) > 4096:
            raise _fail("analytics_credentials_unavailable")
        if not IDENTIFIER.fullmatch(project) or not IDENTIFIER.fullmatch(dataset):
            raise _fail("analytics_identifier_invalid")
        if not 1 <= len(reviewed_tables) <= MAX_TABLES or len(set(reviewed_tables)) != len(reviewed_tables):
            raise _fail("analytics_table_scope_invalid")
        if any(not TABLE_RE.fullmatch(t) for t in reviewed_tables):
            raise _fail("analytics_table_scope_invalid")
        freeze.validate(snapshot.user_id)
        if tuple(t.name for t in snapshot.tables) != reviewed_tables:
            raise _fail("analytics_table_scope_invalid")
        if _digest(snapshot.payload()) != snapshot.snapshot_digest:
            raise _fail("analytics_snapshot_digest_invalid")
        if (
            snapshot.project != project or snapshot.dataset != dataset
            or snapshot.freeze_id != freeze.freeze_id or snapshot.request_id == ""
            or any(t.streaming_buffer for t in snapshot.tables)
        ):
            raise _fail("analytics_snapshot_binding_invalid")
        if _canonical_uuid(snapshot.user_id) != snapshot.user_id:
            raise _fail("analytics_snapshot_binding_invalid")
        try:
            uuid.UUID(snapshot.request_id)
        except (ValueError, TypeError, AttributeError):
            raise _fail("analytics_snapshot_binding_invalid") from None
        if type(timeout_seconds) is bool or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 60:
            raise _fail("analytics_timeout_invalid")
        self._token, self.project, self.dataset = token, project, dataset
        self.snapshot, self.freeze, self.reviewed_tables = snapshot, freeze, reviewed_tables
        self.transport, self.timeout_seconds = transport, timeout_seconds

    def _query(self, table: str, *, count: bool) -> str:
        relation = _relation(self.project, self.dataset, table)
        where = "jsonPayload.user_id = @user_id AND jsonPayload.analytics IS TRUE"
        return f"SELECT COUNT(*) AS remaining FROM {relation} WHERE {where}" if count else f"DELETE FROM {relation} WHERE {where}"

    def _job_id(self, table: str, operation: str) -> str:
        raw = f"{self.snapshot.request_id}:{self.snapshot.snapshot_digest}:{table}:{operation}"
        return "chirp_" + hashlib.sha256(raw.encode()).hexdigest()[:48]

    def _job_body(self, query: str, job_id: str, *, dry_run: bool) -> dict[str, object]:
        return {"jobReference": {"projectId": self.project, "jobId": job_id, "location": LOCATION},
                "configuration": {"query": {
                    "query": query, "useLegacySql": False, "useQueryCache": False,
                    "parameterMode": "NAMED", "dryRun": dry_run,
                    "queryParameters": [{"name": "user_id", "parameterType": {"type": "STRING"},
                                         "parameterValue": {"value": self.snapshot.user_id}}],
                }}}

    def preview_requests(self) -> list[dict[str, object]]:
        return [self._job_body(self._query(t, count=True), self._job_id(t, "preview"), dry_run=True)
                for t in self.reviewed_tables]

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs: object) -> dict[str, object]:
        try:
            response = await client.request(method, path, follow_redirects=False, **kwargs)
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise _fail("analytics_transport_unknown") from None
        if response.status_code in {301, 302, 303, 307, 308} or response.next_request is not None:
            raise _fail("analytics_redirect_rejected")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise _fail("analytics_response_too_large")
        if response.status_code == 409:
            raise _fail("analytics_provider_conflict")
        if response.status_code >= 400:
            raise _fail("analytics_provider_error")
        try:
            payload = response.json()
        except ValueError:
            raise _fail("analytics_response_invalid") from None
        if not isinstance(payload, dict):
            raise _fail("analytics_response_invalid")
        return payload

    async def _metadata(self, client: httpx.AsyncClient, table: str) -> AnalyticsTable:
        payload = await self._request(client, "GET", f"/bigquery/v2/projects/{self.project}/datasets/{self.dataset}/tables/{table}")
        ref = payload.get("tableReference", {})
        if not isinstance(ref, dict) or (ref.get("projectId"), ref.get("datasetId"), ref.get("tableId")) != (self.project, self.dataset, table):
            raise _fail("analytics_table_binding_invalid")
        _validate_schema(payload.get("schema"))
        return AnalyticsTable(table, _digest(payload["schema"]), bool(payload.get("streamingBuffer")))

    async def _validate_current_snapshot(self, client: httpx.AsyncClient) -> None:
        for expected in self.snapshot.tables:
            current = await self._metadata(client, expected.name)
            if current.schema_digest != expected.schema_digest or current.streaming_buffer:
                raise _fail("analytics_snapshot_drift")

    async def capture_snapshot(self) -> AnalyticsSnapshot:
        self.freeze.validate(self.snapshot.user_id)
        async with httpx.AsyncClient(base_url=ORIGIN, headers={"Authorization": f"Bearer {self._token}"},
                                     timeout=self.timeout_seconds, follow_redirects=False, transport=self.transport) as client:
            tables = tuple([await self._metadata(client, table) for table in self.reviewed_tables])
        if any(t.streaming_buffer for t in tables):
            raise _fail("analytics_streaming_buffer_present")
        current = AnalyticsSnapshot(self.snapshot.request_id, self.snapshot.user_id, self.project, self.dataset,
                                    tables, self.freeze.freeze_id, "", datetime.now(timezone.utc).isoformat())
        return AnalyticsSnapshot(**{**current.__dict__, "snapshot_digest": _digest(current.payload())})

    @staticmethod
    def _validate_job_config(job: dict[str, object], body: dict[str, object]) -> None:
        observed = job.get("configuration")
        expected = body.get("configuration")
        if observed != expected:
            raise _fail("analytics_job_config_mismatch")

    async def _job(self, client: httpx.AsyncClient, table: str, operation: str, query: str, *, dry_run: bool) -> dict[str, object]:
        job_id = self._job_id(table, operation)
        body = self._job_body(query, job_id, dry_run=dry_run)
        try:
            job = await self._request(client, "POST", f"/bigquery/v2/projects/{self.project}/jobs", json=body)
        except AnalyticsProviderUnavailable as exc:
            if str(exc) != "analytics_provider_conflict":
                raise
            job = await self._request(client, "GET", f"/bigquery/v2/projects/{self.project}/jobs/{job_id}?location={LOCATION}")
        ref = job.get("jobReference", {})
        if not isinstance(ref, dict) or ref.get("jobId") != job_id or ref.get("projectId") != self.project:
            raise _fail("analytics_job_binding_invalid")
        self._validate_job_config(job, body)
        for _ in range(MAX_POLLS):
            state = job.get("status", {}).get("state")
            if state == "DONE":
                if job.get("status", {}).get("errorResult"):
                    raise _fail("analytics_job_failed")
                return job
            if state not in {"PENDING", "RUNNING"}:
                raise _fail("analytics_job_state_invalid")
            job = await self._request(client, "GET", f"/bigquery/v2/projects/{self.project}/jobs/{job_id}?location={LOCATION}")
        raise _fail("analytics_job_timeout")

    async def delete_account_data(self, *, user_id: uuid.UUID | str, idempotency_key: str) -> dict[str, object]:
        actual = _canonical_uuid(user_id)
        if actual != self.snapshot.user_id or type(idempotency_key) is not str or not idempotency_key:
            raise _fail("analytics_subject_mismatch")
        self.freeze.validate(actual)
        async with asyncio.timeout(self.timeout_seconds * (2 * len(self.reviewed_tables) + 1)):
            async with httpx.AsyncClient(base_url=ORIGIN, headers={"Authorization": f"Bearer {self._token}"},
                                         timeout=self.timeout_seconds, follow_redirects=False, transport=self.transport) as client:
                await self._validate_current_snapshot(client)
                affected = 0
                for table in self.reviewed_tables:
                    self.freeze.validate(actual)
                    job = await self._job(client, table, "delete", self._query(table, count=False), dry_run=False)
                    stats = job.get("statistics", {}).get("query", {}).get("dmlStats", {})
                    raw = stats.get("deletedRowCount") if isinstance(stats, dict) else None
                    if not isinstance(raw, str) or not raw.isdigit() or int(raw) > MAX_ROWS:
                        raise _fail("analytics_affected_count_invalid")
                    affected += int(raw)
                    if affected > MAX_ROWS:
                        raise _fail("analytics_affected_count_invalid")
                    readback = await self._job(client, table, "readback", self._query(table, count=True), dry_run=False)
                    rows = readback.get("rows")
                    if not isinstance(rows, list) or len(rows) != 1:
                        raise _fail("analytics_readback_invalid")
                    fields = rows[0].get("f") if isinstance(rows[0], dict) else None
                    value = fields[0].get("v") if isinstance(fields, list) and fields and isinstance(fields[0], dict) else None
                    if value != "0":
                        raise _fail("analytics_readback_nonzero")
                return {"confirmed": True, "provider": "analytics", "subject": actual,
                        "scope": list(self.reviewed_tables), "readback": True,
                        "retained_with_reason": [], "affected_rows": affected,
                        "snapshot_digest": self.snapshot.snapshot_digest}
