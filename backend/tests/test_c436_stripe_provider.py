"""Dormant Stripe customer adapter tests; no Stripe network calls."""

from __future__ import annotations


from tests.c436_executor_helpers import activate_executor_hold
import asyncio
import uuid
from types import SimpleNamespace

import pytest
import stripe
from sqlalchemy import text, update

from app import models
from app.db import get_session_factory
from app.services.account_fulfillment import ProviderUnavailable
from app.services.account_provider_stripe import (
    StripeCustomerProvider,
    StripeCustomerReference,
    load_customer_references,
    provider_for_user,
)


class FakeCustomer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.deleted: set[tuple[str, str]] = set()
        self.delete_failures = 0
        self.subscription_rows: dict[tuple[str, str], object] = {}
        self.retrieve_override: dict[tuple[str, str], object] = {}
        self.post_delete_override: dict[tuple[str, str], object] = {}

    async def retrieve_async(self, customer_id: str, **params):
        account = params["stripe_account"]
        self.calls.append(("retrieve", customer_id, params))
        key = (account, customer_id)
        if key in self.deleted and key in self.post_delete_override:
            return self.post_delete_override[key]
        if key in self.retrieve_override:
            value = self.retrieve_override[key]
            if isinstance(value, BaseException):
                raise value
            return value
        if key in self.deleted:
            return SimpleNamespace(id=customer_id, object="customer", deleted=True)
        return SimpleNamespace(id=customer_id, object="customer", deleted=False)

    async def delete_async(self, customer_id: str, **params):
        account = params["stripe_account"]
        self.calls.append(("delete", customer_id, params))
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("transient provider failure")
        self.deleted.add((account, customer_id))
        return SimpleNamespace(id=customer_id, object="customer", deleted=True)


class LostDeleteResponseCustomer(FakeCustomer):
    async def delete_async(self, customer_id: str, **params):
        account = params["stripe_account"]
        self.calls.append(("delete", customer_id, params))
        self.deleted.add((account, customer_id))
        raise RuntimeError("response lost after provider mutation")


class FakeSubscription:
    def __init__(self, customer: FakeCustomer) -> None:
        self.customer = customer

    async def list_async(self, **params):
        key = (params["stripe_account"], params["customer"])
        return self.customer.subscription_rows.get(
            key, SimpleNamespace(data=[], has_more=False)
        )


def fake_stripe(customer: FakeCustomer):
    return SimpleNamespace(Customer=customer, Subscription=FakeSubscription(customer))


def provider(user, customer, *refs):
    return StripeCustomerProvider(
        refs or (StripeCustomerReference("acct_owner", "cus_owner"),),
        expected_user_id=user.id,
        stripe_module=fake_stripe(customer),
        api_key="sk_test_fake",
    )


@pytest.mark.asyncio
async def test_loader_is_caller_owned_and_keeps_connected_account(client, make_user, make_chapter_with):
    owner = await make_user("Stripe owner")
    foreign = await make_user("Stripe foreign")
    owner_chapter = await make_chapter_with("president")
    owner_chapter_two = await make_chapter_with("president")
    foreign_chapter = await make_chapter_with("president")
    async with get_session_factory()() as session:
        for chapter, account in ((owner_chapter, "acct_owner"), (owner_chapter_two, "acct_ownertwo"), (foreign_chapter, "acct_foreign")):
            await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(chapter.chapter_id)).values(stripe_account_id=account))
        session.add_all([
            models.ChapterStripeCustomer(user_id=uuid.UUID(owner.id), chapter_id=uuid.UUID(owner_chapter.chapter_id), stripe_customer_id="cus_owner"),
            models.ChapterStripeCustomer(user_id=uuid.UUID(owner.id), chapter_id=uuid.UUID(owner_chapter_two.chapter_id), stripe_customer_id="cus_ownertwo"),
            models.ChapterStripeCustomer(user_id=uuid.UUID(foreign.id), chapter_id=uuid.UUID(foreign_chapter.chapter_id), stripe_customer_id="cus_foreign"),
        ])
        await session.commit()
        refs = await load_customer_references(session, uuid.UUID(owner.id))
    assert set(refs) == {
        StripeCustomerReference("acct_owner", "cus_owner"),
        StripeCustomerReference("acct_ownertwo", "cus_ownertwo"),
    }


@pytest.mark.asyncio
async def test_loader_rejects_customer_shared_with_foreign_user(client, make_user, make_chapter_with):
    owner = await make_user("Stripe shared owner")
    foreign = await make_user("Stripe shared foreign")
    chapter = await make_chapter_with("president")
    async with get_session_factory()() as session:
        await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(chapter.chapter_id)).values(stripe_account_id="acct_shared"))
        session.add_all([
            models.ChapterStripeCustomer(user_id=uuid.UUID(owner.id), chapter_id=uuid.UUID(chapter.chapter_id), stripe_customer_id="cus_shared"),
        ])
        # A second chapter can point at the same connected-account/customer pair.
        other_chapter = await make_chapter_with("president")
        await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(other_chapter.chapter_id)).values(stripe_account_id="acct_shared"))
        session.add(models.ChapterStripeCustomer(user_id=uuid.UUID(foreign.id), chapter_id=uuid.UUID(other_chapter.chapter_id), stripe_customer_id="cus_shared"))
        await session.commit()
        with pytest.raises(ProviderUnavailable, match="stripe_customer_mapping_foreign_owner"):
            await load_customer_references(session, uuid.UUID(owner.id))


async def _financial_mapping(session, user_id: str, chapter_id: str, *, plan: bool = False, intent_status: str | None = None):
    suffix = user_id.replace("-", "")[:12]
    account_id, customer_id = f"acct_{suffix}", f"cus_{suffix}"
    await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(chapter_id)).values(stripe_account_id=account_id))
    session.add(models.ChapterStripeCustomer(user_id=uuid.UUID(user_id), chapter_id=uuid.UUID(chapter_id), stripe_customer_id=customer_id))
    cycle_id = uuid.uuid4()
    await session.execute(text("INSERT INTO dues_cycles (id, chapter_id, name, amount_cents, due_date) VALUES (:id, :chapter, 'c436', 100, '2030-01-01')"), {"id": cycle_id, "chapter": uuid.UUID(chapter_id)})
    if plan:
        await session.execute(text("INSERT INTO dues_payment_plans (id, chapter_id, dues_cycle_id, user_id, total_cents, installment_count, status, created_by) VALUES (:id, :chapter, :cycle, :user, 100, 1, 'active', :user)"), {"id": uuid.uuid4(), "chapter": uuid.UUID(chapter_id), "cycle": cycle_id, "user": uuid.UUID(user_id)})
    if intent_status is not None:
        await session.execute(text("INSERT INTO dues_payment_intents (id, chapter_id, dues_cycle_id, user_id, rail, status, amount_cents, currency) VALUES (:id, :chapter, :cycle, :user, 'card', :status, 100, 'usd')"), {"id": uuid.uuid4(), "chapter": uuid.UUID(chapter_id), "cycle": cycle_id, "user": uuid.UUID(user_id), "status": intent_status})
    await session.commit()
    return account_id, customer_id


@pytest.mark.asyncio
async def test_factory_blocks_active_plan_and_retryable_or_unknown_intent(make_user, make_chapter_with):
    for plan, intent_status, label in ((True, None, "active-plan"), (False, "open", "open"), (False, "failed", "failed")):
        user = await make_user(f"Stripe financial {label}")
        chapter = await make_chapter_with("president")
        async with get_session_factory()() as session:
            await _financial_mapping(session, user.id, chapter.chapter_id, plan=plan, intent_status=intent_status)
            with pytest.raises(ProviderUnavailable, match="stripe_pending_financial_review_required"):
                await provider_for_user(session, models.User(id=uuid.UUID(user.id)))


@pytest.mark.asyncio
async def test_factory_allows_settled_or_canceled_intent_history(make_user, make_chapter_with):
    for status in ("succeeded", "canceled"):
        user = await make_user(f"Stripe settled {status}")
        chapter = await make_chapter_with("president")
        async with get_session_factory()() as session:
            account_id, customer_id = await _financial_mapping(session, user.id, chapter.chapter_id, intent_status=status)
            service = await provider_for_user(session, models.User(id=uuid.UUID(user.id)))
        assert service._references == (StripeCustomerReference(account_id, customer_id),)


@pytest.mark.asyncio
async def test_provider_deletes_exact_refs_and_repeat_is_readback_idempotent(make_user):
    user = await make_user("Stripe adapter")
    customer = FakeCustomer()
    service = provider(user, customer)
    first = await service.delete_account_data(user=user, idempotency_key="stable-step")
    second = await service.delete_account_data(user=user, idempotency_key="stable-step")
    assert first == second == {"confirmed": True, "provider": "payment_provider", "subject": user.id, "scope": ["stripe_customer_records"], "readback": True, "retained_with_reason": ["stripe_deleted_customer_history"]}
    assert [call[0] for call in customer.calls] == ["retrieve", "retrieve", "delete", "retrieve", "retrieve", "retrieve"]
    assert all(call[2]["stripe_account"] == "acct_owner" for call in customer.calls)


@pytest.mark.asyncio
async def test_subject_mismatch_happens_before_provider_calls(make_user):
    owner = await make_user("Stripe owner bound")
    other = await make_user("Stripe other bound")
    customer = FakeCustomer()
    service = provider(owner, customer)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_subject_mismatch"):
        await service.delete_account_data(user=other, idempotency_key="stable-step")
    assert customer.calls == []


@pytest.mark.asyncio
async def test_empty_mapping_is_explicit_and_makes_no_provider_call(make_user):
    user = await make_user("Stripe empty")
    customer = FakeCustomer()
    result = await StripeCustomerProvider([], expected_user_id=user.id, stripe_module=fake_stripe(customer), api_key="sk_test_fake").delete_account_data(user=user, idempotency_key="stable-step")
    assert result["scope"] == ["stripe_customer_records:none_locally_mapped"]
    assert result["retained_with_reason"] == []
    assert customer.calls == []


@pytest.mark.asyncio
async def test_active_or_incomplete_subscription_requires_manual_review(make_user):
    user = await make_user("Stripe active")
    customer = FakeCustomer()
    customer.subscription_rows[("acct_owner", "cus_owner")] = SimpleNamespace(data=[SimpleNamespace(status="active")], has_more=False)
    with pytest.raises(ProviderUnavailable, match="stripe_active_payment_review_required"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")
    assert [call[0] for call in customer.calls] == ["retrieve"]


@pytest.mark.asyncio
async def test_later_active_customer_is_preflighted_before_any_delete(make_user):
    user = await make_user("Stripe preflight")
    customer = FakeCustomer()
    customer.subscription_rows[("acct_second", "cus_second")] = SimpleNamespace(data=[SimpleNamespace(status="active")], has_more=False)
    service = provider(user, customer, StripeCustomerReference("acct_first", "cus_first"), StripeCustomerReference("acct_second", "cus_second"))
    with pytest.raises(ProviderUnavailable, match="stripe_active_payment_review_required"):
        await service.delete_account_data(user=user, idempotency_key="stable-step")
    assert [call[0] for call in customer.calls] == ["retrieve", "retrieve"]
    assert not any(call[0] == "delete" for call in customer.calls)


@pytest.mark.asyncio
async def test_subscription_review_requires_complete_known_data(make_user):
    user = await make_user("Stripe incomplete subscriptions")
    customer = FakeCustomer()
    customer.subscription_rows[("acct_owner", "cus_owner")] = SimpleNamespace(data=[], has_more=True)
    with pytest.raises(ProviderUnavailable, match="stripe_subscription_review_incomplete"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_unknown_subscription_status_blocks_delete(make_user):
    user = await make_user("Stripe unknown subscription")
    customer = FakeCustomer()
    customer.subscription_rows[("acct_owner", "cus_owner")] = SimpleNamespace(data=[SimpleNamespace(status="future_unknown")], has_more=False)
    with pytest.raises(ProviderUnavailable, match="stripe_subscription_status_unknown"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_delete_retry_and_strict_readback(make_user):
    user = await make_user("Stripe retry")
    customer = FakeCustomer()
    customer.delete_failures = 1
    service = provider(user, customer)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_provider_error"):
        await service.delete_account_data(user=user, idempotency_key="stable-step")
    result = await service.delete_account_data(user=user, idempotency_key="stable-step")
    assert result["confirmed"] is True
    assert [call[0] for call in customer.calls] == ["retrieve", "retrieve", "delete", "retrieve", "retrieve", "delete", "retrieve"]


@pytest.mark.asyncio
async def test_lost_delete_response_retries_by_readback_without_second_delete(make_user):
    user = await make_user("Stripe lost response")
    customer = LostDeleteResponseCustomer()
    service = provider(user, customer)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_provider_error"):
        await service.delete_account_data(user=user, idempotency_key="stable-step")
    result = await service.delete_account_data(user=user, idempotency_key="stable-step")
    assert result["confirmed"] is True
    assert [call[0] for call in customer.calls] == ["retrieve", "retrieve", "delete", "retrieve", "retrieve"]


@pytest.mark.asyncio
async def test_post_delete_mismatched_readback_blocks_confirmation(make_user):
    user = await make_user("Stripe post delete mismatch")
    customer = FakeCustomer()
    customer.post_delete_override[("acct_owner", "cus_owner")] = SimpleNamespace(id="cus_other", object="customer", deleted=True)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_readback_failed"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_wrong_deleted_shape_and_mismatched_readback_fail_closed(make_user):
    user = await make_user("Stripe strict shape")
    customer = FakeCustomer()
    customer.retrieve_override[("acct_owner", "cus_owner")] = SimpleNamespace(id="cus_owner", object="customer", deleted="true")
    with pytest.raises(ProviderUnavailable, match="stripe_customer_readback_failed"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")
    customer.retrieve_override[("acct_owner", "cus_owner")] = SimpleNamespace(id="cus_other", object="customer", deleted=False)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_readback_failed"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step-2")


@pytest.mark.asyncio
async def test_resource_missing_is_not_treated_as_deleted(make_user):
    user = await make_user("Stripe missing")

    class MissingCustomer(FakeCustomer):
        async def retrieve_async(self, customer_id: str, **params):
            raise stripe.InvalidRequestError("missing", "id", code="resource_missing", http_status=404)

    with pytest.raises(ProviderUnavailable, match="stripe_customer_provider_error"):
        await provider(user, MissingCustomer()).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_operation_timeout_is_bounded(make_user, monkeypatch):
    user = await make_user("Stripe timeout")
    customer = FakeCustomer()
    original = customer.retrieve_async

    async def slow_retrieve(customer_id: str, **params):
        await asyncio.sleep(0.05)
        return await original(customer_id, **params)

    customer.retrieve_async = slow_retrieve
    import app.services.account_provider_stripe as module
    monkeypatch.setattr(module, "OPERATION_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_operation_timeout"):
        await provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_total_deadline_and_missing_credentials_are_sanitized(make_user, monkeypatch):
    user = await make_user("Stripe deadline")
    customer = FakeCustomer()
    original = customer.retrieve_async

    async def slow_retrieve(customer_id: str, **params):
        await asyncio.sleep(0.05)
        return await original(customer_id, **params)

    customer.retrieve_async = slow_retrieve
    import app.services.account_provider_stripe as module
    monkeypatch.setattr(module, "TOTAL_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(module, "OPERATION_TIMEOUT_SECONDS", 1)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_total_timeout"):
        await StripeCustomerProvider(
            [StripeCustomerReference("acct_owner", "cus_owner")],
            expected_user_id=user.id, stripe_module=fake_stripe(customer), api_key="sk_test_fake",
        ).delete_account_data(user=user, idempotency_key="stable-step")

    class MissingKey:
        @staticmethod
        def _secret_key():
            raise RuntimeError("secret value")

    monkeypatch.setattr("app.services.stripe_service._secret_key", MissingKey._secret_key)
    with pytest.raises(ProviderUnavailable, match="stripe_customer_credentials_unavailable"):
        await StripeCustomerProvider(
            [StripeCustomerReference("acct_owner", "cus_owner")],
            expected_user_id=user.id, stripe_module=fake_stripe(FakeCustomer()), api_key=None,
        ).delete_account_data(user=user, idempotency_key="stable-step")


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_converted_to_success(make_user):
    user = await make_user("Stripe cancelled")
    customer = FakeCustomer()

    async def slow_retrieve(customer_id: str, **params):
        await asyncio.sleep(10)

    customer.retrieve_async = slow_retrieve
    task = asyncio.create_task(provider(user, customer).delete_account_data(user=user, idempotency_key="stable-step"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_reference_validation_is_fail_closed():
    with pytest.raises(ProviderUnavailable, match="stripe_customer_mapping_invalid"):
        StripeCustomerProvider([StripeCustomerReference("acct_owner", "not-a-customer")], expected_user_id=uuid.uuid4())
    with pytest.raises(ProviderUnavailable, match="stripe_customer_mapping_invalid"):
        StripeCustomerProvider([StripeCustomerReference("acct_é", "cus_same")], expected_user_id=uuid.uuid4())
    with pytest.raises(ProviderUnavailable, match="stripe_customer_mapping_ambiguous"):
        StripeCustomerProvider([StripeCustomerReference("acct_owner", "cus_same"), StripeCustomerReference("acct_other", "cus_same")], expected_user_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_real_stripe_adapter_receipt_reaches_durable_fulfillment_result(client, make_user):
    from sqlalchemy import select
    from app.services.account_fulfillment import PROVIDER_RETENTION_REASONS, run_deletion
    from tests.test_c436_fulfillment import provider_set

    owner = await make_user("Stripe retention integration")
    response = await client.post("/me/data-requests", json={"kind": "deletion"}, headers=owner.headers)
    assert response.status_code == 201
    request_id = uuid.UUID(response.json()["id"])
    providers = provider_set()
    customer = FakeCustomer()
    providers["payment_provider"] = provider(owner, customer)
    async with get_session_factory()() as session:
        user = await session.get(models.User, uuid.UUID(owner.id))
        await activate_executor_hold(user_id=user.id, request_id=request_id)
        result = await run_deletion(session, request_id, user, providers)
        assert result.status == "completed"
    async with get_session_factory()() as observer:
        request = await observer.get(models.AccountDataRequest, request_id)
        step = (await observer.scalars(select(models.AccountFulfillmentStep).where(
            models.AccountFulfillmentStep.request_id == request_id,
            models.AccountFulfillmentStep.step_key == "payment_provider",
        ))).one()
        assert step.provider_ref["retained_with_reason"] == ["stripe_deleted_customer_history"]
        assert PROVIDER_RETENTION_REASONS["stripe_deleted_customer_history"] in request.retention_reasons
        assert request.status == "completed"
    assert [call[0] for call in customer.calls].count("delete") == 1
