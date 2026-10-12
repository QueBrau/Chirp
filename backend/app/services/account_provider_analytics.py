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
STDOUT_TABLE_RE = re.compile(r"^run_googleapis_com_stdout_[0-9]{8}$")
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
    preview_count: int = 0
    max_bytes_billed: int = 0

    def validate(self) -> None:
        if type(self.preview_count) is not int or self.preview_count < 0 or type(self.max_bytes_billed) is not int or self.max_bytes_billed < 10_000_000:
            raise _fail("analytics_snapshot_budget_invalid")


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
    excluded_tables: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id, "user_id": self.user_id,
            "project": self.project, "dataset": self.dataset,
            "tables": [{"name": t.name, "schema_digest": t.schema_digest,
                        "streaming_buffer": t.streaming_buffer,
                        "preview_count": t.preview_count,
                        "max_bytes_billed": t.max_bytes_billed} for t in self.tables],
            "excluded_tables": list(self.excluded_tables),
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
    if not isinstance(payload, dict) or payload.get("type") != "RECORD" or payload.get("mode", "NULLABLE") != "NULLABLE":
        raise _fail("analytics_schema_invalid")
    nested = _fields(payload)
    if (nested.get("user_id", {}).get("type"), nested.get("user_id", {}).get("mode", "NULLABLE")) != ("STRING", "NULLABLE") or (nested.get("analytics", {}).get("type"), nested.get("analytics", {}).get("mode", "NULLABLE")) != ("BOOLEAN", "NULLABLE"):
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
        if type(token) is not str or not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise _fail("analytics_credentials_unavailable")
        if not IDENTIFIER.fullmatch(project) or not IDENTIFIER.fullmatch(dataset):
            raise _fail("analytics_identifier_invalid")
        if not 1 <= len(reviewed_tables) <= MAX_TABLES or len(set(reviewed_tables)) != len(reviewed_tables):
            raise _fail("analytics_table_scope_invalid")
        if any(not TABLE_RE.fullmatch(t) for t in reviewed_tables):
            raise _fail("analytics_table_scope_invalid")
        freeze.validate(snapshot.user_id)
        if tuple(t.name for t in snapshot.tables) != reviewed_tables or any(not STDOUT_TABLE_RE.fullmatch(t) for t in reviewed_tables):
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
        if sum(t.preview_count for t in snapshot.tables) > MAX_ROWS:
            raise _fail("analytics_snapshot_row_budget_exceeded")
        for table in snapshot.tables:
            table.validate()
        self.transport, self.timeout_seconds = transport, min(timeout_seconds, 120.0)

    def _query(self, table: str, *, count: bool, reviewed_count: int | None = None) -> str:
        relation = _relation(self.project, self.dataset, table)
        where = "jsonPayload.user_id = @user_id AND jsonPayload.analytics IS TRUE"
        if count:
            return f"SELECT COUNT(*) AS remaining FROM {relation} WHERE {where}"
        if reviewed_count is None or type(reviewed_count) is not int or reviewed_count < 0:
            raise _fail("analytics_snapshot_row_budget_invalid")
        return f"DELETE FROM {relation} WHERE {where} AND (SELECT COUNT(*) FROM {relation} WHERE {where}) = @reviewed_count"

    def _job_id(self, table: str, operation: str) -> str:
        raw = f"{self.snapshot.request_id}:{self.snapshot.snapshot_digest}:{table}:{operation}"
        return "chirp_" + hashlib.sha256(raw.encode()).hexdigest()[:48]

    def _job_body(self, query: str, job_id: str, *, dry_run: bool, reviewed_count: int | None = None, max_bytes_billed: int | None = None) -> dict[str, object]:
        params = [{"name": "user_id", "parameterType": {"type": "STRING"}, "parameterValue": {"value": self.snapshot.user_id}}]
        if reviewed_count is not None:
            params.append({"name": "reviewed_count", "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(reviewed_count)}})
        query_config = {"query": query, "useLegacySql": False, "useQueryCache": False,
                        "parameterMode": "NAMED", "queryParameters": params}
        if max_bytes_billed is not None:
            query_config["maximumBytesBilled"] = str(max_bytes_billed)
        return {"jobReference": {"projectId": self.project, "jobId": job_id, "location": LOCATION},
                "configuration": {"dryRun": dry_run, "query": query_config}}

    def preview_requests(self) -> list[dict[str, object]]:
        return [self._job_body(self._query(t, count=True), self._job_id(t, "preview"), dry_run=True)
                for t in self.reviewed_tables]

    @classmethod
    async def capture_preview_snapshot(
        cls, *, token: str, project: str, dataset: str, request_id: str,
        user_id: uuid.UUID | str, freeze: AnalyticsFreezeReceipt,
        reviewed_tables: tuple[str, ...], excluded_tables: tuple[str, ...] = (),
        transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 20.0,
    ) -> AnalyticsSnapshot:
        """Capture metadata for a fresh operator-reviewed scope.

        Construction deliberately does not accept a preexisting snapshot. The
        caller must review the returned table inventory and preview counts
        before constructing the destructive adapter.
        """
        actual = _canonical_uuid(user_id)
        if actual != freeze.user_id:
            raise _fail("analytics_freeze_invalid")
        freeze.validate(actual)
        if type(request_id) is not str:
            raise _fail("analytics_snapshot_binding_invalid")
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise _fail("analytics_snapshot_binding_invalid") from None
        if not reviewed_tables or len(reviewed_tables) > MAX_TABLES or any(not STDOUT_TABLE_RE.fullmatch(t) for t in reviewed_tables):
            raise _fail("analytics_table_scope_invalid")
        if any(not TABLE_RE.fullmatch(t) for t in excluded_tables):
            raise _fail("analytics_table_scope_invalid")
        if type(timeout_seconds) is bool or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 60:
            raise _fail("analytics_timeout_invalid")
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=ORIGIN, headers=headers, timeout=timeout_seconds,
                                     follow_redirects=False, trust_env=False, transport=transport) as client:
            dataset_body = await client.get(f"/bigquery/v2/projects/{project}/datasets/{dataset}", follow_redirects=False)
            if dataset_body.status_code >= 400:
                raise _fail("analytics_provider_error")
            dataset_info = dataset_body.json()
            if not isinstance(dataset_info, dict) or dataset_info.get("location") != LOCATION:
                raise _fail("analytics_dataset_binding_invalid")
            seen: list[str] = []
            page: str | None = None
            while True:
                suffix = "?maxResults=100" + (f"&pageToken={page}" if page else "")
                payload = await cls._inventory_request(client, project, dataset, suffix)
                rows = payload.get("tables")
                if not isinstance(rows, list) or len(seen) + len(rows) > MAX_TABLES:
                    raise _fail("analytics_table_inventory_invalid")
                for row in rows:
                    if not isinstance(row, dict) or row.get("type") != "TABLE":
                        raise _fail("analytics_table_inventory_invalid")
                    ref = row.get("tableReference", {})
                    name = ref.get("tableId") if isinstance(ref, dict) else None
                    if not isinstance(name, str) or name in seen or not (STDOUT_TABLE_RE.fullmatch(name) or name in excluded_tables):
                        raise _fail("analytics_table_inventory_invalid")
                    seen.append(name)
                page = payload.get("nextPageToken")
                if page is None:
                    break
                if not isinstance(page, str) or not page:
                    raise _fail("analytics_table_inventory_invalid")
            if set(seen) != set(reviewed_tables) | set(excluded_tables):
                raise _fail("analytics_table_inventory_incomplete")
            tables = []
            for name in reviewed_tables:
                ref = await cls._metadata_request(client, project, dataset, name)
                if "streamingBuffer" in ref:
                    raise _fail("analytics_streaming_buffer_present")
                _validate_schema(ref.get("schema"))
                tables.append(AnalyticsTable(name, _digest(ref["schema"]), False))
            # Build a valid metadata-only instance solely to execute bounded,
            # read-only COUNT previews. It is never returned as the snapshot.
            provisional = AnalyticsSnapshot(request_id, actual, project, dataset, tuple(AnalyticsTable(t.name, t.schema_digest, False, 0, 10_000_000) for t in tables), freeze.freeze_id, "", datetime.now(timezone.utc).isoformat(), tuple(excluded_tables))
            provisional = AnalyticsSnapshot(**{**provisional.__dict__, "snapshot_digest": _digest(provisional.payload())})
            previewer = cls(token=token, project=project, dataset=dataset, snapshot=provisional,
                            freeze=freeze, reviewed_tables=tuple(reviewed_tables), transport=transport,
                            timeout_seconds=timeout_seconds)
            for index, table in enumerate(tuple(tables)):
                job = await previewer._job(client, table.name, "preview", previewer._query(table.name, count=True), dry_run=False, max_bytes_billed=10_000_000)
                result = await previewer._query_results(client, job, previewer._job_id(table.name, "preview"))
                row = result["rows"][0]["f"][0]["v"]
                if not isinstance(row, str) or not row.isdigit() or int(row) > MAX_ROWS:
                    raise _fail("analytics_preview_count_invalid")
                stats = job.get("statistics", {}).get("query", {}) if isinstance(job.get("statistics"), dict) else {}
                processed = stats.get("totalBytesProcessed", "0") if isinstance(stats, dict) else "0"
                if not isinstance(processed, str) or not processed.isdigit() or int(processed) > 10_000_000:
                    raise _fail("analytics_preview_budget_invalid")
                tables[index] = AnalyticsTable(table.name, table.schema_digest, False, int(row), int(processed))
        current = AnalyticsSnapshot(request_id, actual, project, dataset, tuple(tables), freeze.freeze_id, "", datetime.now(timezone.utc).isoformat(), tuple(excluded_tables))
        return AnalyticsSnapshot(**{**current.__dict__, "snapshot_digest": _digest(current.payload())})

    @staticmethod
    async def _inventory_request(client: httpx.AsyncClient, project: str, dataset: str, suffix: str) -> dict[str, object]:
        response = await client.get(f"/bigquery/v2/projects/{project}/datasets/{dataset}/tables{suffix}", follow_redirects=False)
        if response.status_code >= 400:
            raise _fail("analytics_provider_error")
        try:
            payload = response.json()
        except ValueError:
            raise _fail("analytics_response_invalid") from None
        if not isinstance(payload, dict):
            raise _fail("analytics_response_invalid")
        return payload

    @staticmethod
    async def _metadata_request(client: httpx.AsyncClient, project: str, dataset: str, table: str) -> dict[str, object]:
        response = await client.get(f"/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}", follow_redirects=False)
        if response.status_code >= 400:
            raise _fail("analytics_provider_error")
        try:
            payload = response.json()
        except ValueError:
            raise _fail("analytics_response_invalid") from None
        if not isinstance(payload, dict):
            raise _fail("analytics_response_invalid")
        return payload

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
        if payload.get("type") != "TABLE" or not isinstance(ref, dict) or (ref.get("projectId"), ref.get("datasetId"), ref.get("tableId")) != (self.project, self.dataset, table):
            raise _fail("analytics_table_binding_invalid")
        _validate_schema(payload.get("schema"))
        if "streamingBuffer" in payload:
            raise _fail("analytics_streaming_buffer_present")
        return AnalyticsTable(table, _digest(payload["schema"]), False)

    async def _validate_current_snapshot(self, client: httpx.AsyncClient) -> None:
        for expected in self.snapshot.tables:
            current = await self._metadata(client, expected.name)
            if current.schema_digest != expected.schema_digest or current.streaming_buffer:
                raise _fail("analytics_snapshot_drift")

    async def capture_snapshot(self) -> AnalyticsSnapshot:
        self.freeze.validate(self.snapshot.user_id)
        async with httpx.AsyncClient(base_url=ORIGIN, headers={"Authorization": f"Bearer {self._token}"},
                                     timeout=self.timeout_seconds, follow_redirects=False, trust_env=False,
                                     transport=self.transport) as client:
            tables = tuple([await self._metadata(client, table) for table in self.reviewed_tables])
        if any(t.streaming_buffer for t in tables):
            raise _fail("analytics_streaming_buffer_present")
        current = AnalyticsSnapshot(self.snapshot.request_id, self.snapshot.user_id, self.project, self.dataset,
                                    tables, self.freeze.freeze_id, "", datetime.now(timezone.utc).isoformat())
        return AnalyticsSnapshot(**{**current.__dict__, "snapshot_digest": _digest(current.payload())})

    @staticmethod
    def _validate_job_config(job: dict[str, object], body: dict[str, object]) -> None:
        observed_cfg = job.get("configuration", {})
        expected_cfg = body.get("configuration", {})
        observed = observed_cfg.get("query", {}) if isinstance(observed_cfg, dict) else {}
        expected = expected_cfg.get("query", {}) if isinstance(expected_cfg, dict) else {}
        if (not isinstance(observed_cfg, dict) or observed_cfg.get("dryRun") is not expected_cfg.get("dryRun")
                or not isinstance(observed, dict) or any(observed.get(key) != value for key, value in expected.items())):
            raise _fail("analytics_job_config_mismatch")

    async def _job(self, client: httpx.AsyncClient, table: str, operation: str, query: str, *, dry_run: bool, reviewed_count: int | None = None, max_bytes_billed: int | None = None, job_id: str | None = None) -> dict[str, object]:
        job_id = job_id or self._job_id(table, operation)
        body = self._job_body(query, job_id, dry_run=dry_run, reviewed_count=reviewed_count, max_bytes_billed=max_bytes_billed)
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
            status = job.get("status", {})
            state = status.get("state") if isinstance(status, dict) else None
            if state == "DONE":
                if job.get("status", {}).get("errorResult"):
                    raise _fail("analytics_job_failed")
                return job
            if state not in {"PENDING", "RUNNING"}:
                raise _fail("analytics_job_state_invalid")
            job = await self._request(client, "GET", f"/bigquery/v2/projects/{self.project}/jobs/{job_id}?location={LOCATION}")
        raise _fail("analytics_job_timeout")

    async def _query_results(self, client: httpx.AsyncClient, job: dict[str, object], job_id: str) -> dict[str, object]:
        ref = job.get("jobReference", {})
        if not isinstance(ref, dict) or ref.get("projectId") != self.project or ref.get("location") != LOCATION or ref.get("jobId") != job_id:
            raise _fail("analytics_job_binding_invalid")
        result = await self._request(
            client, "GET", f"/bigquery/v2/projects/{self.project}/queries/{job_id}?location={LOCATION}&maxResults=1",
        )
        result_ref = result.get("jobReference", {})
        if result.get("jobComplete") is not True or not isinstance(result_ref, dict) or any(result_ref.get(k) != ref.get(k) for k in ("projectId", "location", "jobId")):
            raise _fail("analytics_query_result_invalid")
        if result.get("totalRows") != "1":
            raise _fail("analytics_query_result_invalid")
        schema = result.get("schema", {})
        fields = schema.get("fields", []) if isinstance(schema, dict) else []
        rows = result.get("rows")
        if not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict) or fields[0].get("name") != "remaining" or fields[0].get("type") != "INTEGER" or not isinstance(rows, list) or len(rows) != 1:
            raise _fail("analytics_query_result_invalid")
        return result

    async def delete_account_data(self, *, user_id: uuid.UUID | str, idempotency_key: str) -> dict[str, object]:
        actual = _canonical_uuid(user_id)
        if actual != self.snapshot.user_id or type(idempotency_key) is not str or not idempotency_key:
            raise _fail("analytics_subject_mismatch")
        self.freeze.validate(actual)
        async with asyncio.timeout(min(120.0, self.timeout_seconds * (2 * len(self.reviewed_tables) + 1))):
            if idempotency_key != f"c436:{self.snapshot.request_id}":
                raise _fail("analytics_idempotency_key_invalid")
            async with httpx.AsyncClient(base_url=ORIGIN, headers={"Authorization": f"Bearer {self._token}"},
                                         timeout=self.timeout_seconds, follow_redirects=False, trust_env=False,
                                         transport=self.transport) as client:
                await self._validate_current_snapshot(client)
                affected = 0
                for table in self.reviewed_tables:
                    self.freeze.validate(actual)
                    expected_count = next(t.preview_count for t in self.snapshot.tables if t.name == table)
                    job = await self._job(client, table, "delete", self._query(table, count=False, reviewed_count=expected_count), dry_run=False, reviewed_count=expected_count, max_bytes_billed=next(t.max_bytes_billed for t in self.snapshot.tables if t.name == table))
                    stats = job.get("statistics", {}).get("query", {}).get("dmlStats", {})
                    raw = stats.get("deletedRowCount") if isinstance(stats, dict) else None
                    if not isinstance(raw, str) or not raw.isdigit() or int(raw) > MAX_ROWS:
                        raise _fail("analytics_affected_count_invalid")
                    affected += int(raw)
                    if affected > MAX_ROWS:
                        raise _fail("analytics_affected_count_invalid")
                    readback_id = self._job_id(table, "readback") + "_" + uuid.uuid4().hex[:12]
                    readback_job = await self._job(client, table, "readback", self._query(table, count=True), dry_run=False, job_id=readback_id, max_bytes_billed=next(t.max_bytes_billed for t in self.snapshot.tables if t.name == table))
                    readback = await self._query_results(client, readback_job, readback_id)
                    rows = readback.get("rows")
                    if not isinstance(rows, list) or len(rows) != 1:
                        raise _fail("analytics_readback_invalid")
                    fields = rows[0].get("f") if isinstance(rows[0], dict) else None
                    value = fields[0].get("v") if isinstance(fields, list) and fields and isinstance(fields[0], dict) else None
                    if value != "0":
                        raise _fail("analytics_readback_nonzero")
                return {"confirmed": True, "provider": "analytics", "subject": actual,
                        "scope": list(self.reviewed_tables), "readback": True,
                        "retention_disclosure": "bigquery_time_travel_and_fail_safe_retention; analytics scope is not permanent byte erasure",
                        "affected_rows": affected,
                        "snapshot_digest": self.snapshot.snapshot_digest}
