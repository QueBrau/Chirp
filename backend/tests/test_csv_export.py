"""Shared CSV export helper (app.core.csv_export) + the ledger/meetings CSV export routes.

Covers: CSV/formula-injection neutralization, RFC4180 quoting, a negative ledger amount
surviving export unmangled, role gates on both export routes, filter passthrough on the
ledger export, corrects_entry_id linkage surviving export, and cross-chapter isolation.
"""
from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.csv_export import build_csv, sanitize_csv_text
from tests.conftest import MakeChapterWith


def _parse_csv(text: str) -> list[list[str]]:
    """Parse a CSV response body (header row included) via the stdlib reader, not string split."""
    return list(csv.reader(io.StringIO(text)))


def _row_dicts(text: str) -> list[dict[str, str]]:
    """Parse a CSV response body into header-keyed dicts, one per data row."""
    rows = _parse_csv(text)
    header, data = rows[0], rows[1:]
    return [dict(zip(header, row)) for row in data]


async def _post_ledger_entry(
    client: AsyncClient, chapter_id: str, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"entry_type": "expense", "amount_cents": -1000, **overrides}
    response = await client.post(f"/chapters/{chapter_id}/ledger", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_meeting_with_attendance(
    client: AsyncClient,
    chapter_id: str,
    secretary_headers: dict[str, str],
    members: list[tuple[str, str]],
    title: str = "Chapter Meeting",
    meeting_date: str = "2026-01-15T18:00:00Z",
) -> str:
    created = await client.post(
        f"/chapters/{chapter_id}/meetings",
        json={"title": title, "meeting_date": meeting_date},
        headers=secretary_headers,
    )
    assert created.status_code == 201, created.text
    meeting_id = created.json()["id"]
    if members:
        entries = [{"user_id": user_id, "status": status} for user_id, status in members]
        attendance = await client.put(
            f"/chapters/{chapter_id}/meetings/{meeting_id}/attendance",
            json={"entries": entries},
            headers=secretary_headers,
        )
        assert attendance.status_code == 200, attendance.text
    return meeting_id


# ---------------------------------------------------------------------------
# app.core.csv_export unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_sanitize_csv_text_neutralizes_dangerous_prefixes(prefix: str) -> None:
    """Every OWASP-listed formula-trigger prefix gets a leading apostrophe."""
    value = f"{prefix}cmd|'/c calc'!A0"
    assert sanitize_csv_text(value) == f"'{value}"


def test_sanitize_csv_text_leaves_safe_text_untouched() -> None:
    """Ordinary free text with no dangerous leading char passes through unchanged."""
    assert sanitize_csv_text("Mixer decorations") == "Mixer decorations"


def test_sanitize_csv_text_none_and_empty_become_empty_string() -> None:
    assert sanitize_csv_text(None) == ""
    assert sanitize_csv_text("") == ""


def test_build_csv_does_not_mangle_a_preformatted_negative_number() -> None:
    """build_csv() never sanitizes cells — callers pass pre-formatted numeric cells straight
    through, so a legitimate negative amount ("positive = in, negative = out") must come out
    byte-for-byte, not quoted or reinterpreted."""
    text = build_csv(["amount"], [["-12.50"]])
    rows = _parse_csv(text)
    assert rows[1] == ["-12.50"]


def test_build_csv_rfc4180_quotes_comma_and_doublequote() -> None:
    """A field with an embedded comma and double quote round-trips through a real CSV parser."""
    raw = 'Snacks, "on the house"'
    text = build_csv(["description"], [[raw]])
    assert '"Snacks, ""on the house"""' in text
    rows = _parse_csv(text)
    assert rows[1] == [raw]


# ---------------------------------------------------------------------------
# GET /chapters/{chapter_id}/ledger/export.csv
# ---------------------------------------------------------------------------


async def test_ledger_export_plain_member_is_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


async def test_ledger_export_treasurer_and_president_allowed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")

    treasurer_response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    assert treasurer_response.status_code == 200, treasurer_response.text
    assert treasurer_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in treasurer_response.headers["content-disposition"]

    president_response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.president.headers
    )
    assert president_response.status_code == 200, president_response.text


async def test_ledger_export_neutralizes_injection_and_preserves_negative_amount(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The most important test in this file: a formula-injection description gets neutralized
    while a negative (expense) amount on the SAME row survives export byte-for-byte."""
    setup = await make_chapter_with("treasurer")
    await _post_ledger_entry(
        client,
        setup.chapter_id,
        setup.member.headers,
        entry_type="expense",
        amount_cents=-5000,
        category="social",
        description="=cmd|'/c calc'!A0",
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    rows = _row_dicts(response.text)
    assert len(rows) == 1
    row = rows[0]

    # CRITICAL SUBTLETY: the negative amount must NOT be neutralized — "-50.00" verbatim,
    # not "'-50.00". Sanitizing numeric columns would corrupt every expense in the ledger.
    assert row["amount"] == "-50.00"
    # The malicious description IS neutralized with a leading apostrophe.
    assert row["description"] == "'=cmd|'/c calc'!A0"


async def test_ledger_export_quotes_description_with_comma_and_doublequote(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")
    raw_description = 'Snacks, "on the house"'
    await _post_ledger_entry(
        client, setup.chapter_id, setup.member.headers, description=raw_description
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    rows = _row_dicts(response.text)
    assert rows[0]["description"] == raw_description


async def test_ledger_export_applies_category_filter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")
    await _post_ledger_entry(
        client, setup.chapter_id, setup.member.headers, category="social", description="Mixer"
    )
    await _post_ledger_entry(
        client,
        setup.chapter_id,
        setup.member.headers,
        category="philanthropy",
        description="Donation",
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv",
        params={"category": "social"},
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    rows = _row_dicts(response.text)
    assert {row["category"] for row in rows} == {"social"}


async def test_ledger_export_includes_corrects_entry_id_linkage(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The ledger is append-only; a correction's link to the original must survive export."""
    setup = await make_chapter_with("treasurer")
    original = await _post_ledger_entry(
        client,
        setup.chapter_id,
        setup.member.headers,
        entry_type="expense",
        amount_cents=-12000,
        category="formal",
    )
    correction = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": 12000,
            "description": "Reversing duplicate expense",
            "corrects_entry_id": original["id"],
        },
        headers=setup.member.headers,
    )
    assert correction.status_code == 201, correction.text

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    rows = {row["entry_type"]: row for row in _row_dicts(response.text)}
    assert rows["correction"]["corrects_entry_id"] == original["id"]
    assert rows["expense"]["corrects_entry_id"] == ""


async def test_ledger_export_related_member_and_created_by_names(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("treasurer")
    await _post_ledger_entry(
        client,
        setup.chapter_id,
        setup.member.headers,
        entry_type="dues_payment",
        amount_cents=5000,
        related_user_id=setup.president.id,
    )
    await _post_ledger_entry(client, setup.chapter_id, setup.member.headers)  # no related_user_id

    response = await client.get(
        f"/chapters/{setup.chapter_id}/ledger/export.csv", headers=setup.member.headers
    )
    rows = _row_dicts(response.text)
    related_names = {row["related_member"] for row in rows}
    assert "Chapter President" in related_names
    assert "" in related_names  # blank, not an error, when related_user_id is null
    assert all(row["created_by"] == "Chapter Treasurer" for row in rows)


async def test_ledger_export_cross_chapter_isolation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    chapter_a = await make_chapter_with("treasurer")
    chapter_b = await make_chapter_with("treasurer")
    await _post_ledger_entry(
        client, chapter_a.chapter_id, chapter_a.member.headers, description="Chapter A only"
    )
    await _post_ledger_entry(
        client, chapter_b.chapter_id, chapter_b.member.headers, description="Chapter B only"
    )

    response = await client.get(
        f"/chapters/{chapter_a.chapter_id}/ledger/export.csv", headers=chapter_a.member.headers
    )
    assert response.status_code == 200, response.text
    assert "Chapter A only" in response.text
    assert "Chapter B only" not in response.text

    # A treasurer of chapter A can't even reach chapter B's export (org scoping, §8.4).
    cross_chapter = await client.get(
        f"/chapters/{chapter_b.chapter_id}/ledger/export.csv", headers=chapter_a.member.headers
    )
    assert cross_chapter.status_code == 403, cross_chapter.text
    assert cross_chapter.json() == {"detail": "not_a_member"}


# ---------------------------------------------------------------------------
# GET /chapters/{chapter_id}/meetings/export.csv
# ---------------------------------------------------------------------------


async def test_meetings_export_plain_member_is_403(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


async def test_meetings_export_secretary_and_president_allowed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")

    secretary_response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv", headers=setup.member.headers
    )
    assert secretary_response.status_code == 200, secretary_response.text
    assert secretary_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in secretary_response.headers["content-disposition"]

    president_response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv", headers=setup.president.headers
    )
    assert president_response.status_code == 200, president_response.text


async def test_meetings_export_long_format_one_row_per_attendance_record(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Two meetings x two members == four attendance rows — a long table, not a pivoted matrix."""
    setup = await make_chapter_with("secretary")
    await _create_meeting_with_attendance(
        client,
        setup.chapter_id,
        setup.member.headers,
        members=[(setup.member.id, "present"), (setup.president.id, "excused")],
        title="Chapter Meeting 1",
        meeting_date="2026-01-15T18:00:00Z",
    )
    await _create_meeting_with_attendance(
        client,
        setup.chapter_id,
        setup.member.headers,
        members=[(setup.member.id, "absent"), (setup.president.id, "present")],
        title="Chapter Meeting 2",
        meeting_date="2026-02-15T18:00:00Z",
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv", headers=setup.member.headers
    )
    assert response.status_code == 200, response.text
    rows = _parse_csv(response.text)
    header, data = rows[0], rows[1:]
    assert header == ["meeting_date", "meeting_title", "member", "status"]
    assert len(data) == 4


async def test_meetings_export_neutralizes_injection_in_title(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    await _create_meeting_with_attendance(
        client,
        setup.chapter_id,
        setup.member.headers,
        members=[(setup.member.id, "present")],
        title="=cmd|'/c calc'!A0",
    )

    response = await client.get(
        f"/chapters/{setup.chapter_id}/meetings/export.csv", headers=setup.member.headers
    )
    rows = _row_dicts(response.text)
    assert rows[0]["meeting_title"] == "'=cmd|'/c calc'!A0"


async def test_meetings_export_cross_chapter_isolation(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    chapter_a = await make_chapter_with("secretary")
    chapter_b = await make_chapter_with("secretary")
    await _create_meeting_with_attendance(
        client,
        chapter_a.chapter_id,
        chapter_a.member.headers,
        members=[(chapter_a.member.id, "present")],
        title="Chapter A Meeting",
    )
    await _create_meeting_with_attendance(
        client,
        chapter_b.chapter_id,
        chapter_b.member.headers,
        members=[(chapter_b.member.id, "present")],
        title="Chapter B Meeting",
    )

    response = await client.get(
        f"/chapters/{chapter_a.chapter_id}/meetings/export.csv", headers=chapter_a.member.headers
    )
    assert response.status_code == 200, response.text
    assert "Chapter A Meeting" in response.text
    assert "Chapter B Meeting" not in response.text

    cross_chapter = await client.get(
        f"/chapters/{chapter_b.chapter_id}/meetings/export.csv", headers=chapter_a.member.headers
    )
    assert cross_chapter.status_code == 403, cross_chapter.text
    assert cross_chapter.json() == {"detail": "not_a_member"}
