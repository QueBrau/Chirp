"""Strict, bounded inputs for the opt-in local message-receipt instrument."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from loadtest.accounts import Manifest, VirtualUser
from loadtest.config import HarnessConfig

MAX_INPUT_BYTES = 64 * 1024
MAX_USERS = 20
MAX_RECIPIENTS = 10
MAX_MESSAGES = 10
MAX_TOTAL_FRAMES = 1000
MAX_HTTP_SAMPLES = 5000
OVERALL_TIMEOUT_SECONDS = 180.0
UID_PATTERN = re.compile(r"receipt-[A-Za-z0-9_-]{1,88}\Z")


class ReceiptError(ValueError):
    """Only fixed diagnostic codes, never input values or upstream bodies."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("duplicate_json_key")
        result[key] = value
    return result


def strict_json(raw: str | bytes, *, limit: int = MAX_INPUT_BYTES):
    if not isinstance(raw, (str, bytes)) or len(raw) > limit:
        raise ReceiptError("json_size_or_type_invalid")
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReceiptError("invalid_json")))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ReceiptError("invalid_json") from None


def keys(value, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ReceiptError("object_shape_invalid")
    return value


def canonical_uuid(value) -> str:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ReceiptError("uuid_invalid")
    return value


def timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ReceiptError("timestamp_invalid") from None


def uid(value) -> str:
    if not isinstance(value, str) or UID_PATTERN.fullmatch(value) is None:
        raise ReceiptError("uid_invalid")
    return value


@dataclass(frozen=True)
class ReceiptManifest:
    http: Manifest
    sender: VirtualUser
    sender_device_id: str
    conversation_id: str
    recipients: tuple[VirtualUser, ...]


def parse_manifest(raw: str | bytes) -> ReceiptManifest:
    data = keys(strict_json(raw), {"schema_version", "campus_id", "chapter_id", "users", "message_workload"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ReceiptError("manifest_schema_invalid")
    campus, chapter = canonical_uuid(data["campus_id"]), canonical_uuid(data["chapter_id"])
    if not isinstance(data["users"], list) or not 2 <= len(data["users"]) <= MAX_USERS:
        raise ReceiptError("user_count_invalid")
    users, user_ids = {}, set()
    for row in data["users"]:
        keys(row, {"uid", "user_id"})
        identifier, user_id = uid(row["uid"]), canonical_uuid(row["user_id"])
        if identifier in users or user_id in user_ids:
            raise ReceiptError("duplicate_manifest_identity")
        users[identifier] = VirtualUser(identifier)
        user_ids.add(user_id)
    workload = keys(data["message_workload"], {
        "sender_uid", "sender_device_id", "conversation_id", "recipient_uids",
    })
    sender = uid(workload["sender_uid"])
    recipients = workload["recipient_uids"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise ReceiptError("recipient_count_invalid")
    recipients = [uid(value) for value in recipients]
    if (sender not in users or len(set(recipients)) != len(recipients)
            or sender in recipients or any(value not in users for value in recipients)):
        raise ReceiptError("recipient_cohort_invalid")
    return ReceiptManifest(
        Manifest(campus, chapter, list(users.values())), users[sender],
        canonical_uuid(workload["sender_device_id"]), canonical_uuid(workload["conversation_id"]),
        tuple(users[value] for value in recipients),
    )


def read_manifest(path: str | Path) -> ReceiptManifest:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise ReceiptError("manifest_unreadable") from None
    return parse_manifest(raw)


def validate_target(value: str, *, websocket: bool) -> None:
    scheme, path = ("ws", "/ws") if websocket else ("http", "")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        host_text = "[::1]" if host == "::1" else host
        valid = (
            isinstance(value, str) and host in {"127.0.0.1", "::1"}
            and parsed.scheme == scheme and parsed.port is not None
            and 1 <= parsed.port <= 65535 and parsed.path == path
            and value == f"{scheme}://{host_text}:{parsed.port}{path}"
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ReceiptError("requires_literal_loopback_target")


def validate_run(config: HarnessConfig, manifest: ReceiptManifest, messages: int,
                 interval_seconds: float, settle_seconds: float) -> None:
    # These checks always precede construction of any HTTP or WebSocket client.
    validate_target(config.base_url, websocket=False)
    validate_target(config.ws_url, websocket=True)
    if config.auth_mode != "emulated":
        raise ReceiptError("requires_emulated_auth")
    try:
        config.validate(confirm_park_lifted=False)
    except (SystemExit, ValueError, TypeError):
        raise ReceiptError("harness_config_invalid") from None
    if (not 2 <= len(manifest.http.users) <= MAX_USERS
            or config.duration_seconds > 120 or config.caps.max_rps > 20
            or config.caps.max_concurrent_requests > 10
            or len(manifest.recipients) > config.ws.max_sockets
            or not .5 <= config.ws.connects_per_second <= 10):
        raise ReceiptError("local_budget_exceeded")
    if type(messages) is not int or not 1 <= messages <= MAX_MESSAGES:
        raise ReceiptError("message_count_invalid")
    if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (float, int))
            or not math.isfinite(interval_seconds) or not 4 <= interval_seconds <= 60):
        raise ReceiptError("message_interval_invalid")
    if (isinstance(settle_seconds, bool) or not isinstance(settle_seconds, (float, int))
            or not math.isfinite(settle_seconds) or not .1 <= settle_seconds <= 30):
        raise ReceiptError("settle_interval_invalid")
    if (messages - 1) * interval_seconds >= config.duration_seconds:
        raise ReceiptError("message_schedule_exceeds_http_duration")
