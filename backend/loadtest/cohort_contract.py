"""Bounded private fixture contract for the separate local cohort instrument."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from loadtest.receipt_contract import (
    ReceiptError, ReceiptManifest, canonical_uuid, keys, parse_manifest as parse_receipt,
    strict_json, timestamp, validate_run,
)

MAX_MANIFEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_COHORTS = 4
PAGE_SIZE = 50


@dataclass(frozen=True)
class Cohort:
    receipt: ReceiptManifest
    member_ids: frozenset[str]
    post_ids: frozenset[str]
    chapter_post_ids: frozenset[str]
    conversation_ids: frozenset[str]
    message_ids: frozenset[str]
    active_ids: frozenset[str]
    history_before: str


def _ids(value, allowed_sizes) -> frozenset[str]:
    if not isinstance(value, list) or len(value) not in allowed_sizes:
        raise ReceiptError("dataset_cardinality_invalid")
    result = frozenset(canonical_uuid(item) for item in value)
    if len(result) != len(value):
        raise ReceiptError("dataset_identity_duplicate")
    return result


def parse_manifest(raw: str | bytes) -> tuple[Cohort, ...]:
    data = keys(strict_json(raw, limit=MAX_MANIFEST_BYTES), {"schema_version", "cohorts"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ReceiptError("cohort_schema_invalid")
    rows = data["cohorts"]
    if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_COHORTS:
        raise ReceiptError("cohort_count_invalid")
    result, seen_ids, seen_uids = [], set(), set()
    for row in rows:
        keys(row, {"receipt", "dataset"})
        receipt = parse_receipt(json.dumps(row["receipt"]))
        dataset = keys(row["dataset"], {
            "member_ids", "post_ids", "chapter_post_ids", "conversation_ids", "message_ids", "history_before",
        })
        members = _ids(dataset["member_ids"], {100, 300})
        posts = _ids(dataset["post_ids"], {51, 201})
        chapter_posts = _ids(dataset["chapter_post_ids"], {51, 201})
        conversations = _ids(dataset["conversation_ids"], {51, 201})
        messages = _ids(dataset["message_ids"], {51, 201})
        if len({len(posts), len(chapter_posts), len(conversations), len(messages)}) != 1:
            raise ReceiptError("dataset_profile_mismatch")
        active = frozenset(user["user_id"] for user in row["receipt"]["users"])
        if not active <= members or receipt.conversation_id not in conversations:
            raise ReceiptError("dataset_receipt_binding_invalid")
        timestamp(dataset["history_before"])
        # The only intended repeated identities are the selected members and
        # receipt conversation within this same cohort's dataset.
        anchors = {receipt.http.campus_id, receipt.http.chapter_id, receipt.sender_device_id}
        if len(anchors) != 3:
            raise ReceiptError("cohort_identity_overlap")
        identity_groups = [members, posts, chapter_posts, conversations, messages, anchors]
        identities = set().union(*identity_groups)
        uids = {user.uid for user in receipt.http.users}
        if (sum(map(len, identity_groups)) != len(identities)
                or seen_ids & identities or seen_uids & uids):
            raise ReceiptError("cohort_identity_overlap")
        seen_ids.update(identities)
        seen_uids.update(uids)
        result.append(Cohort(receipt, members, posts, chapter_posts, conversations, messages,
                             active, dataset["history_before"]))
    if sum(len(c.receipt.http.users) for c in result) > 20 or sum(len(c.receipt.recipients) for c in result) > 10:
        raise ReceiptError("aggregate_user_budget_exceeded")
    return tuple(result)


def read_manifest(path: str | Path) -> tuple[Cohort, ...]:
    try:
        with open(path, "rb") as handle:
            return parse_manifest(handle.read(MAX_MANIFEST_BYTES + 1))
    except OSError:
        raise ReceiptError("cohort_manifest_unreadable") from None


def validate_cohort_run(config, cohorts, messages, interval_seconds, settle_seconds):
    if not isinstance(cohorts, tuple) or not 2 <= len(cohorts) <= MAX_COHORTS:
        raise ReceiptError("cohort_count_invalid")
    if type(messages) is not int or not 1 <= messages <= 3 or messages * len(cohorts) > 10:
        raise ReceiptError("aggregate_message_budget_exceeded")
    for cohort in cohorts:
        validate_run(config, cohort.receipt, messages, interval_seconds, settle_seconds)
    if (sum(len(c.receipt.http.users) for c in cohorts) > 20
            or sum(len(c.receipt.recipients) for c in cohorts) > min(10, config.ws.max_sockets)):
        raise ReceiptError("aggregate_user_budget_exceeded")
    if (messages * len(cohorts) - 1) * interval_seconds >= config.duration_seconds:
        raise ReceiptError("aggregate_message_schedule_exceeds_duration")
    # This command has a fixed read plan. Do not silently substitute its mix
    # for a caller's weighted workload configuration.
    if config.mix_weights != {"me": 1} or config.ramp_in_seconds != 0:
        raise ReceiptError("cohort_requires_fixed_read_plan")
