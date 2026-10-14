"""Real-PostgreSQL coverage for finance deletion-hold admission."""
from __future__ import annotations

import uuid
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold
from app.services import finance_write_admission
from tests.conftest import MakeChapterWith


def _set_admission(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    yield
    get_settings.cache_clear()


async def _hold(user_id: str) -> None:
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id),
            kind="deletion",
            open_key=f"finance-{uuid.uuid4()}",
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        await session.commit()
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()


async def _create_cycle(client, setup, *, name: str = "Fall dues") -> dict:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={"name": name, "amount_cents": 30_000, "due_date": "2027-11-01"},
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _plan_body(user_id: str) -> dict:
    installments = [
        {"amount_cents": 10_000, "due_date": f"2027-{month:02d}-01"}
        for month in (9, 10, 11)
    ]
    return {
        "user_id": user_id,
        "installment_count": 3,
        "installments": installments,
    }


async def _create_plan(client, setup, cycle_id: str, user_id: str) -> dict:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle_id}/plans",
        json=_plan_body(user_id),
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_spend(client, setup, *, creator=None) -> dict:
    creator = creator or setup.president
    response = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals",
        json={"amount_cents": 2_500, "description": "Club supplies"},
        headers=creator.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _counts(chapter_id: str) -> dict[str, int]:
    async with get_session_factory()() as session:
        out = {}
        for key, model in (
            ("cycles", models.DuesCycle),
            ("ledger", models.LedgerEntry),
            ("spend", models.SpendApproval),
            ("plans", models.DuesPaymentPlan),
            ("installments", models.DuesPlanInstallment),
        ):
            out[key] = int(
                await session.scalar(
                    select(func.count()).select_from(model).where(
                        model.chapter_id == uuid.UUID(chapter_id)
                        if hasattr(model, "chapter_id")
                        else model.plan_id.in_(
                            select(models.DuesPaymentPlan.id).where(
                                models.DuesPaymentPlan.chapter_id
                                == uuid.UUID(chapter_id)
                            )
                        )
                    )
                )
            )
    return out


@pytest.mark.asyncio
async def test_held_actor_cannot_write_any_finance_route_but_retained_reads_work(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    cycle = await _create_cycle(client, setup)
    plan = await _create_plan(client, setup, cycle["id"], setup.member.id)
    approval = await _create_spend(client, setup)
    before = await _counts(setup.chapter_id)
    await _hold(setup.president.id)

    cycle_create = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={"name": "Must not persist", "amount_cents": 30_000, "due_date": "2027-11-01"},
        headers=setup.president.headers,
    )
    ledger_append = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={"entry_type": "expense", "amount_cents": -1_000, "category": "supplies"},
        headers=setup.president.headers,
    )
    spend_create = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals",
        json={"amount_cents": 500, "description": "Must not persist"},
        headers=setup.president.headers,
    )
    spend_decide = await client.post(
        f"/chapters/{setup.chapter_id}/spend-approvals/{approval['id']}/decide",
        json={"status": "approved"},
        headers=setup.president.headers,
    )
    plan_create = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle['id']}/plans",
        json=_plan_body(setup.member.id),
        headers=setup.president.headers,
    )
    installment_pay = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan['id']}/installments/1/record-payment",
        json={},
        headers=setup.president.headers,
    )
    plan_cancel = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{plan['id']}/cancel",
        headers=setup.president.headers,
    )
    responses = [
        cycle_create, ledger_append, spend_create, spend_decide,
        plan_create, installment_pay, plan_cancel,
    ]
    assert [response.status_code for response in responses] == [423] * len(responses)
    assert await _counts(setup.chapter_id) == before

    cycles_read = await client.get(
        f"/chapters/{setup.chapter_id}/dues-cycles", headers=setup.president.headers
    )
    ledger_read = await client.get(
        f"/chapters/{setup.chapter_id}/ledger", headers=setup.president.headers
    )
    spend_read = await client.get(
        f"/chapters/{setup.chapter_id}/spend-approvals", headers=setup.president.headers
    )
    assert [r.status_code for r in (cycles_read, ledger_read, spend_read)] == [200, 200, 200]
    assert len(cycles_read.json()) == 1
    assert len(ledger_read.json()) == 0
    assert len(spend_read.json()) == 1 and spend_read.json()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_held_finance_target_blocks_new_plan_but_preserves_existing_history(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    pay_cycle = await _create_cycle(client, setup, name="Installment cycle")
    ledger_cycle = await _create_cycle(client, setup, name="Manual settlement cycle")
    cancel_cycle = await _create_cycle(client, setup, name="Cancellation cycle")
    pay_plan = await _create_plan(client, setup, pay_cycle["id"], setup.member.id)
    cancel_plan = await _create_plan(client, setup, cancel_cycle["id"], setup.member.id)
    await _hold(setup.member.id)

    refused = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{ledger_cycle['id']}/plans",
        json=_plan_body(setup.member.id),
        headers=setup.president.headers,
    )
    assert refused.status_code == 423, refused.text

    paid = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{pay_plan['id']}/installments/1/record-payment",
        json={"note": "cash received"},
        headers=setup.president.headers,
    )
    canceled = await client.post(
        f"/chapters/{setup.chapter_id}/dues-plans/{cancel_plan['id']}/cancel",
        headers=setup.president.headers,
    )
    settled = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": 5_000,
            "related_user_id": setup.member.id,
            "dues_cycle_id": ledger_cycle["id"],
        },
        headers=setup.president.headers,
    )
    assert paid.status_code == 200, paid.text
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    assert settled.status_code == 201, settled.text
    assert paid.json()["ledger_entry_id"]
    assert settled.json()["related_user_id"] == setup.member.id

    async with get_session_factory()() as session:
        target_paid_row = await session.get(
            models.LedgerEntry, uuid.UUID(paid.json()["ledger_entry_id"])
        )
        settlement_row = await session.get(models.LedgerEntry, uuid.UUID(settled.json()["id"]))
        canceled_row = await session.get(
            models.DuesPaymentPlan, uuid.UUID(cancel_plan["id"])
        )
    assert target_paid_row.related_user_id == uuid.UUID(setup.member.id)
    assert settlement_row.related_user_id == uuid.UUID(setup.member.id)
    assert canceled_row.status == "canceled"


@pytest.mark.asyncio
async def test_plan_preflight_keeps_role_privacy_and_refuses_deleted_target(
    client, make_chapter_with: MakeChapterWith, make_user, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    cycle = await _create_cycle(client, setup)
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member"}, headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    deleted_target = await make_user("completed finance target")
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]},
        headers=deleted_target.headers,
    )
    assert joined.status_code == 201, joined.text
    await _hold(setup.member.id)

    unauthorized = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle['id']}/plans",
        json=_plan_body(setup.member.id),
        headers=setup.member.headers,
    )
    assert unauthorized.status_code == 403
    assert unauthorized.json() == {"detail": "insufficient_role"}

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE users SET firebase_uid = :uid, email = :email, "
                "is_ghost = true WHERE id = :id"
            ),
            {
                "uid": f"deleted:{deleted_target.id}",
                "email": f"deleted+{deleted_target.id}@invalid.chirp",
                "id": deleted_target.id,
            },
        )
        await session.commit()

    deleted_target = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle['id']}/plans",
        json=_plan_body(deleted_target.id),
        headers=setup.president.headers,
    )
    assert deleted_target.status_code == 422, deleted_target.text
    assert deleted_target.json() == {"detail": "dues_plan_target_deleted"}
    assert await _counts(setup.chapter_id) == {
        "cycles": 1, "ledger": 0, "spend": 0, "plans": 0, "installments": 0,
    }


@pytest.mark.asyncio
async def test_plan_target_hold_entering_after_preflight_refuses_without_writing(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    cycle = await _create_cycle(client, setup)
    original_factory = finance_write_admission.get_session_factory
    held_after_preflight = False

    class _PreflightManager:
        def __init__(self, manager):
            self.manager = manager

        async def __aenter__(self):
            return await self.manager.__aenter__()

        async def __aexit__(self, exc_type, exc, traceback):
            nonlocal held_after_preflight
            result = await self.manager.__aexit__(exc_type, exc, traceback)
            if not held_after_preflight:
                # The preflight transaction, including its target FOR UPDATE, is
                # fully closed; activate a real target hold before the dependency
                # binds and starts the guarded request transaction.
                held_after_preflight = True
                await _hold(setup.member.id)
            return result

    class _FactoryProxy:
        def __call__(self):
            return _PreflightManager(original_factory()())

    monkeypatch.setattr(
        finance_write_admission, "get_session_factory", lambda: _FactoryProxy()
    )
    refused = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles/{cycle['id']}/plans",
        json=_plan_body(setup.member.id),
        headers=setup.president.headers,
    )
    assert held_after_preflight
    assert refused.status_code == 423, refused.text
    assert await _counts(setup.chapter_id) == {
        "cycles": 1, "ledger": 0, "spend": 0, "plans": 0, "installments": 0,
    }


@pytest.mark.asyncio
async def test_flag_off_plan_creation_ignores_holds_without_hold_table_queries(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    cycle = await _create_cycle(client, setup)
    await _hold(setup.president.id)
    await _hold(setup.member.id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        created = await client.post(
            f"/chapters/{setup.chapter_id}/dues-cycles/{cycle['id']}/plans",
            json=_plan_body(setup.member.id),
            headers=setup.president.headers,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert created.status_code == 201, created.text
    assert not any("account_deletion_holds" in statement for statement in statements)
    assert await _counts(setup.chapter_id) == {
        "cycles": 1, "ledger": 0, "spend": 0, "plans": 1, "installments": 3,
    }


@pytest.mark.asyncio
async def test_flag_off_preserves_held_finance_write_without_hold_table_query(
    client, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, False)
    setup = await make_chapter_with("member")
    await _hold(setup.president.id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        created = await client.post(
            f"/chapters/{setup.chapter_id}/dues-cycles",
            json={"name": "Legacy allowed", "amount_cents": 30_000, "due_date": "2027-11-01"},
            headers=setup.president.headers,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert created.status_code == 201, created.text
    assert not any("account_deletion_holds" in statement for statement in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write_kind",
    ["cycle", "ledger", "spend_create", "spend_decide", "plan", "installment", "cancel"],
)
async def test_finance_response_is_ready_before_hold_after_commit(
    write_kind: str,
    client,
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admission(monkeypatch, True)
    setup = await make_chapter_with("member")
    cycle = await _create_cycle(client, setup)
    plan = await _create_plan(client, setup, cycle["id"], setup.member.id)
    approval = await _create_spend(client, setup, creator=setup.member)
    second_cycle = None
    if write_kind == "plan":
        second_cycle = await _create_cycle(client, setup, name="Post-commit plan cycle")
    original_commit = AsyncSession.commit
    activated = False

    async def commit_then_activate_hold(session: AsyncSession, *args, **kwargs):
        nonlocal activated
        result = await original_commit(session, *args, **kwargs)
        if not activated:
            activated = True
            await _hold(setup.president.id)
        return result

    monkeypatch.setattr(AsyncSession, "commit", commit_then_activate_hold)
    chapter = f"/chapters/{setup.chapter_id}"
    if write_kind == "cycle":
        response = await client.post(
            f"{chapter}/dues-cycles",
            json={"name": "Post-commit cycle", "amount_cents": 30_000, "due_date": "2027-11-01"},
            headers=setup.president.headers,
        )
    elif write_kind == "ledger":
        response = await client.post(
            f"{chapter}/ledger",
            json={"entry_type": "expense", "amount_cents": -500, "category": "supplies"},
            headers=setup.president.headers,
        )
    elif write_kind == "spend_create":
        response = await client.post(
            f"{chapter}/spend-approvals",
            json={"amount_cents": 700, "description": "Post-commit spend"},
            headers=setup.president.headers,
        )
    elif write_kind == "spend_decide":
        response = await client.post(
            f"{chapter}/spend-approvals/{approval['id']}/decide",
            json={"status": "approved"},
            headers=setup.president.headers,
        )
    elif write_kind == "plan":
        response = await client.post(
            f"{chapter}/dues-cycles/{second_cycle['id']}/plans",
            json=_plan_body(setup.member.id),
            headers=setup.president.headers,
        )
    elif write_kind == "installment":
        response = await client.post(
            f"{chapter}/dues-plans/{plan['id']}/installments/1/record-payment",
            json={},
            headers=setup.president.headers,
        )
    else:
        response = await client.post(
            f"{chapter}/dues-plans/{plan['id']}/cancel",
            headers=setup.president.headers,
        )

    assert activated
    expected_status = 201 if write_kind in {"cycle", "ledger", "spend_create", "plan"} else 200
    assert response.status_code == expected_status, response.text
    result = response.json()
    async with get_session_factory()() as session:
        if write_kind == "cycle":
            cycle_row = await session.get(models.DuesCycle, uuid.UUID(result["id"]))
            assert cycle_row is not None and cycle_row.name == "Post-commit cycle"
        elif write_kind == "ledger":
            ledger_row = await session.get(models.LedgerEntry, uuid.UUID(result["id"]))
            assert ledger_row is not None and ledger_row.amount_cents == -500
        elif write_kind in {"spend_create", "spend_decide"}:
            spend_row = await session.get(models.SpendApproval, uuid.UUID(result["id"]))
            assert spend_row is not None
            expected = "approved" if write_kind == "spend_decide" else "pending"
            assert spend_row.status == expected
        elif write_kind == "plan":
            plan_row = await session.get(models.DuesPaymentPlan, uuid.UUID(result["id"]))
            installment_count = await session.scalar(
                select(func.count()).select_from(models.DuesPlanInstallment).where(
                    models.DuesPlanInstallment.plan_id == uuid.UUID(result["id"])
                )
            )
            assert plan_row is not None and plan_row.status == "active"
            assert installment_count == 3
        elif write_kind == "installment":
            installment_row = await session.get(
                models.DuesPlanInstallment, uuid.UUID(result["id"])
            )
            assert installment_row is not None and installment_row.ledger_entry_id is not None
        else:
            plan_row = await session.get(models.DuesPaymentPlan, uuid.UUID(result["id"]))
            assert plan_row is not None and plan_row.status == "canceled"

