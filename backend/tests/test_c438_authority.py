"""Versioned authority records bind to actual memberships and provider identities."""
import uuid

import pytest
from sqlalchemy import select, update

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.role_term_service import apply_role_change
from tests.conftest import set_campus
from tests.test_payments import stripe_env, stripe_calls, FakeStripeObject


async def declaration(client, setup):
    result = await client.get(f"/chapters/{setup.chapter_id}/payments/authority", headers=setup.president.headers)
    assert result.status_code == 200, result.text
    context = result.json()
    return {key: value for key, value in context.items() if key not in {"chapter_id", "org_name"}} | {"confirmed": True}


async def accept(client, user):
    result = await client.post('/auth/legal-acceptance', headers=user.headers, json={
        'terms_version': '2026-10-06', 'privacy_version': '2026-10-06', 'age_declaration': 18,
    })
    assert result.status_code == 200, result.text


async def test_founder_authority_is_explicit_versioned_and_does_not_grant_moderation(client, make_user, make_campus, monkeypatch):
    founder = await make_user('Authority founder')
    campus = await make_campus()
    await set_campus(founder.id, campus)
    await accept(client, founder)
    monkeypatch.setattr(get_settings(), 'legal_enforcement_enabled', True)
    body = {'org_name': 'Authorized org'}
    missing = await client.post('/chapters', headers=founder.headers, json=body)
    assert missing.status_code == 428 and missing.json()['detail'] == 'organization_authority_required'
    body['authority'] = {'policy_version': 'old', 'confirmed': True}
    assert (await client.post('/chapters', headers=founder.headers, json=body)).status_code == 409
    body['authority'] = {'policy_version': '2026-10-06', 'confirmed': False}
    assert (await client.post('/chapters', headers=founder.headers, json=body)).status_code == 422
    body['authority']['confirmed'] = True
    created = await client.post('/chapters', headers=founder.headers, json=body)
    assert created.status_code == 201, created.text
    async with get_session_factory()() as session:
        row = (await session.scalars(select(models.OrganizationAuthorityAcceptance))).one()
        assert str(row.user_id) == founder.id and str(row.chapter_id) == created.json()['id']
        assert row.purpose == 'organization_create' and row.role == 'president'
        assert row.policy_version == '2026-10-06' and row.stripe_account_id is None
        assert row.accepted_at is not None
        term = await session.get(models.RoleTerm, row.role_term_id)
        assert term.membership_id == row.membership_id
    assert (await client.get('/moderation/reports', headers=founder.headers)).status_code == 403


async def test_payment_declaration_binds_created_account_and_stale_snapshot_cannot_resume(client, make_chapter_with, stripe_env, stripe_calls, monkeypatch):
    setup = await make_chapter_with('president')
    await accept(client, setup.president)
    context = await declaration(client, setup)
    monkeypatch.setattr(get_settings(), 'legal_enforcement_enabled', True)
    missing = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id})
    assert missing.status_code == 428 and missing.json()['detail'] == 'organization_authority_required'
    assert not stripe_calls['account_create'] and not stripe_calls['account_link']
    body = {'chapter_id': setup.chapter_id, 'authority': context}
    created = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json=body)
    assert created.status_code == 200, created.text
    async with get_session_factory()() as session:
        rows = (await session.scalars(select(models.OrganizationAuthorityAcceptance))).all()
        chapter = await session.get(models.Chapter, uuid.UUID(setup.chapter_id))
        assert {r.stripe_account_id for r in rows} == {None, chapter.stripe_account_id}
        assert len(rows) == 2 and all(str(r.user_id) == setup.president.id for r in rows)
        assert all(str(r.role_term_id) == context['role_term_id'] and r.purpose == 'payment_setup' for r in rows)
    stale = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json=body)
    assert stale.status_code == 409 and stale.json()['detail'] == 'organization_authority_changed'
    assert len(stripe_calls['account_create']) == len(stripe_calls['account_link']) == 1
    body['authority'] = await declaration(client, setup)
    assert (await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json=body)).status_code == 200
    assert len(stripe_calls['account_create']) == 1 and len(stripe_calls['account_link']) == 2


async def test_role_roundtrip_and_foreign_scope_invalidate_old_confirmation(client, make_chapter_with, stripe_env, stripe_calls):
    setup = await make_chapter_with('member')
    context = await declaration(client, setup)
    async with get_session_factory()() as session:
        member = await session.get(models.Membership, uuid.UUID(context['membership_id']))
        for role in ('treasurer', 'president'):
            await apply_role_change(session, membership=member, new_role=role, changed_by=member.user_id)
            await session.flush()
        await session.commit()
    stale = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id, 'authority': context})
    assert stale.status_code == 409
    refused = await client.get(f'/chapters/{setup.chapter_id}/payments/authority', headers=setup.member.headers)
    assert refused.status_code == 403
    fresh = await declaration(client, setup)
    assert fresh['role_term_id'] != context['role_term_id']
    fresh['membership_id'] = str(uuid.uuid4())
    assert (await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id, 'authority': fresh})).status_code == 409
    assert not stripe_calls['account_create'] and not stripe_calls['account_link']


async def test_revoked_role_during_provider_call_never_binds_account_or_returns_link(client, make_chapter_with, stripe_env, stripe_calls, monkeypatch):
    import stripe
    setup = await make_chapter_with('president')
    context = await declaration(client, setup)
    async def revoked(**kwargs):
        async with get_session_factory()() as session:
            await session.execute(update(models.Membership).where(models.Membership.id == uuid.UUID(context['membership_id'])).values(status='removed'))
            await session.commit()
        return FakeStripeObject(id='acct_authority_revoked')
    monkeypatch.setattr(stripe.Account, 'create_async', revoked)
    response = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id, 'authority': context})
    assert response.status_code == 403
    assert not stripe_calls['account_link']
    async with get_session_factory()() as session:
        chapter = await session.get(models.Chapter, uuid.UUID(setup.chapter_id))
        assert chapter.stripe_account_id is None


async def test_provider_binding_change_during_link_call_never_returns_old_account_link(client, make_chapter_with, stripe_env, stripe_calls, monkeypatch):
    import stripe
    setup = await make_chapter_with('president')
    async with get_session_factory()() as session:
        await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(setup.chapter_id)).values(stripe_account_id='acct_initial'))
        await session.commit()
    context = await declaration(client, setup)
    async def changed(**kwargs):
        async with get_session_factory()() as session:
            await session.execute(update(models.Chapter).where(models.Chapter.id == uuid.UUID(setup.chapter_id)).values(stripe_account_id='acct_replacement'))
            await session.commit()
        return FakeStripeObject(url='https://connect.stripe.test/obsolete', expires_at=1800000000)
    monkeypatch.setattr(stripe.AccountLink, 'create_async', changed)
    response = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id, 'authority': context})
    assert response.status_code == 409 and 'url' not in response.json()


async def test_old_policy_is_rejected_even_with_compatibility_flag_off(client, make_chapter_with, stripe_env, stripe_calls):
    setup = await make_chapter_with('president')
    context = await declaration(client, setup)
    context['policy_version'] = 'old'
    response = await client.post('/payments/connect/onboarding-link', headers=setup.president.headers, json={'chapter_id': setup.chapter_id, 'authority': context})
    assert response.status_code == 409 and response.json()['detail'] == 'organization_policy_changed'
    assert not stripe_calls['account_create'] and not stripe_calls['account_link']
