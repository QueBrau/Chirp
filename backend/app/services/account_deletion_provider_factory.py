"""Explicit, operator-only provider construction for account deletion.

No credentials or cloud SDKs are discovered on import. The CLI calls these
functions only after its database, hold, drain, approval, and plan checks.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from typing import Any

from app import models
from app.config import get_settings
from app.services import account_provider_analytics as analytics
from app.services import account_provider_gcs as media
from app.services.account_provider_retention import EmailLogsBackupsRetentionProvider
from app.services.account_provider_stripe import (
    StripeCustomerProvider,
    StripeCustomerReference,
)
from app.services.account_fulfillment import FirebaseProvider, PROVIDER_STEPS

FIREBASE_PROJECT_ID = "chirps-prod"
ANALYTICS_PROJECT_ID = "chirps-prod"
ANALYTICS_DATASET_ID = "chirp_analytics"
MAX_PLAN_SECONDS = 900


def serialize_media_snapshot(snapshot: media.MediaSnapshot) -> dict[str, Any]:
    return {**snapshot.payload(), "snapshot_digest": snapshot.snapshot_digest}


def deserialize_media_snapshot(value: object) -> media.MediaSnapshot:
    if type(value) is not dict or set(value) != {
        "user_id", "bucket", "prefixes", "objects", "freeze_id", "snapshot_digest",
    }:
        raise ValueError("media_snapshot_invalid")
    objects = value.get("objects")
    prefixes = value.get("prefixes")
    if type(objects) is not list or type(prefixes) is not list:
        raise ValueError("media_snapshot_invalid")
    parsed_objects = tuple(media.MediaObject(**row) for row in objects if type(row) is dict)
    if len(parsed_objects) != len(objects):
        raise ValueError("media_snapshot_invalid")
    snapshot = media.MediaSnapshot(
        user_id=value["user_id"], bucket=value["bucket"],
        prefixes=tuple(prefixes), objects=parsed_objects,
        freeze_id=value["freeze_id"], snapshot_digest=value["snapshot_digest"],
    )
    if (type(snapshot.user_id) is not str or type(snapshot.bucket) is not str
            or type(snapshot.freeze_id) is not str or type(snapshot.snapshot_digest) is not str
            or media._digest(snapshot.payload()) != snapshot.snapshot_digest):
        raise ValueError("media_snapshot_invalid")
    return snapshot


def serialize_analytics_snapshot(snapshot: analytics.AnalyticsSnapshot) -> dict[str, Any]:
    # Match the JSON on-disk representation before local validation too: tuples
    # become lists in the plan file and the strict parser intentionally accepts
    # only that canonical shape.
    return json.loads(json.dumps(asdict(snapshot), sort_keys=True, allow_nan=False))


def deserialize_analytics_snapshot(value: object) -> analytics.AnalyticsSnapshot:
    if type(value) is not dict or set(value) != {
        "request_id", "user_id", "project", "dataset", "tables", "exclusions",
        "freeze_id", "created_at", "time_travel_hours", "snapshot_digest",
    }:
        raise ValueError("analytics_snapshot_invalid")
    tables, exclusions = value.get("tables"), value.get("exclusions")
    if type(tables) is not list or type(exclusions) is not list:
        raise ValueError("analytics_snapshot_invalid")
    try:
        parsed_tables = tuple(analytics.AnalyticsTable(**item) for item in tables)
        parsed_exclusions = tuple(analytics.AnalyticsExclusion(**item) for item in exclusions)
        snapshot = analytics.AnalyticsSnapshot(
            request_id=value["request_id"], user_id=value["user_id"],
            project=value["project"], dataset=value["dataset"],
            tables=parsed_tables, exclusions=parsed_exclusions,
            freeze_id=value["freeze_id"], created_at=value["created_at"],
            time_travel_hours=value["time_travel_hours"],
            snapshot_digest=value["snapshot_digest"],
        )
        snapshot.validate()
    except (TypeError, ValueError, KeyError):
        raise ValueError("analytics_snapshot_invalid") from None
    return snapshot


def analytics_freeze_from_approval(value: object, *, user_id: uuid.UUID) -> analytics.AnalyticsFreezeReceipt:
    if type(value) is not dict or set(value) != {
        "user_id", "freeze_id", "expires_at", "verified", "ingestion_drained",
    }:
        raise ValueError("analytics_freeze_invalid")
    freeze = analytics.AnalyticsFreezeReceipt(**value)
    freeze.validate(str(user_id), require_drained=False)
    if freeze.ingestion_drained is not False:
        raise ValueError("analytics_freeze_invalid")
    return freeze


async def capture_provider_snapshots(
    *, session, user: models.User, request_id: uuid.UUID, approval: dict[str, Any],
) -> dict[str, Any]:
    """Capture the complete reviewed external scope without issuing deletes."""
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request as GoogleAuthRequest

    bucket = get_settings().media_bucket_name
    if not bucket or approval["bucket"] != bucket:
        raise ValueError("media_bucket_binding_invalid")

    refs = await _load_stripe_refs(session, user.id)
    gcs_credentials, gcs_project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/devstorage.full_control"],
    )
    if gcs_project not in (None, FIREBASE_PROJECT_ID):
        raise ValueError("google_project_binding_invalid")
    reader = media.GCSMediaReader(bucket, project=FIREBASE_PROJECT_ID, credentials=gcs_credentials)
    retention = reader.reviewed_retention(
        expected_soft_delete_seconds=approval["soft_delete_seconds"],
        expected_versioning_enabled=False,
    )
    media_references = sorted(await media._owned_references(session, user.id, bucket))
    media_snapshot = await media.snapshot_user_media(
        session, user.id, reader, None, bucket=bucket, deferred_cleanup=True,
    )

    analytics_credentials, analytics_project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    if analytics_project not in (None, ANALYTICS_PROJECT_ID):
        raise ValueError("google_project_binding_invalid")
    analytics_credentials.refresh(GoogleAuthRequest())
    token = analytics_credentials.token
    if type(token) is not str or not token:
        raise ValueError("analytics_credentials_invalid")
    freeze = analytics_freeze_from_approval(approval["analytics_freeze"], user_id=user.id)
    exclusions = tuple(analytics.AnalyticsExclusion(**row) for row in approval["analytics_exclusions"])
    snapshot = await analytics.BigQueryAnalyticsProvider.capture_preview_snapshot(
        token=token, project=ANALYTICS_PROJECT_ID, dataset=ANALYTICS_DATASET_ID,
        request_id=request_id, user_id=user.id, freeze=freeze,
        reviewed_tables=tuple(approval["analytics_tables"]), exclusions=exclusions,
        deferred_cleanup=True,
    )
    return {
        "stripe_references": [asdict(ref) for ref in refs],
        "media_snapshot": serialize_media_snapshot(media_snapshot),
        "media_references": media_references,
        "media_retention": {
            "bucket": retention.bucket,
            "versioning_enabled": retention.versioning_enabled,
            "soft_delete_seconds": retention.soft_delete_seconds,
            "verified": retention.verified,
        },
        "analytics_snapshot": serialize_analytics_snapshot(snapshot),
        "analytics_freeze": asdict(freeze),
    }


async def _load_stripe_refs(session, user_id: uuid.UUID) -> tuple[StripeCustomerReference, ...]:
    from app.services.account_provider_stripe import load_customer_references
    return await load_customer_references(session, user_id)


async def build_deletion_providers(
    *, user_id: uuid.UUID, request_id: uuid.UUID, snapshot_bundle: dict[str, Any],
) -> tuple[dict[str, object], Any]:
    """Build real providers only after CLI preflight; return Firebase app for disposal."""
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import initialize_app
    from google.auth import default as google_auth_default
    from google.auth.transport.requests import Request as GoogleAuthRequest

    bucket = get_settings().media_bucket_name
    if not bucket:
        raise ValueError("media_bucket_binding_invalid")
    credential, project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/devstorage.full_control"],
    )
    if project not in (None, FIREBASE_PROJECT_ID):
        raise ValueError("google_project_binding_invalid")
    reader = media.GCSMediaReader(bucket, project=FIREBASE_PROJECT_ID, credentials=credential)
    retention_row = snapshot_bundle["media_retention"]
    planned_retention = media.MediaRetentionReview(**retention_row)
    # Bucket policy is mutable provider state. Re-read it immediately before
    # building the destructive adapter instead of treating plan-time readback
    # as current proof.
    retention = reader.reviewed_retention(
        expected_soft_delete_seconds=planned_retention.soft_delete_seconds,
        expected_versioning_enabled=planned_retention.versioning_enabled,
    )
    if retention != planned_retention or not retention.verified:
        raise ValueError("media_retention_changed")
    media_snapshot = deserialize_media_snapshot(snapshot_bundle["media_snapshot"])

    bq_credential, bq_project = google_auth_default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    if bq_project not in (None, ANALYTICS_PROJECT_ID):
        raise ValueError("google_project_binding_invalid")
    bq_credential.refresh(GoogleAuthRequest())
    token = bq_credential.token
    analytics_snapshot = deserialize_analytics_snapshot(snapshot_bundle["analytics_snapshot"])
    analytics_freeze = analytics_freeze_from_approval(
        snapshot_bundle["analytics_freeze"], user_id=user_id,
    )
    stripe_refs = tuple(StripeCustomerReference(**row) for row in snapshot_bundle["stripe_references"])
    stripe_key = get_settings().stripe_secret_key
    if type(stripe_key) is not str or not stripe_key:
        raise ValueError("stripe_credentials_invalid")

    app = initialize_app(
        firebase_credentials.ApplicationDefault(),
        options={"projectId": FIREBASE_PROJECT_ID, "httpTimeout": 30},
        name=f"c436-{request_id.hex[:12]}",
    )
    settings = get_settings()
    providers = {
        "payment_provider": StripeCustomerProvider(
            stripe_refs, expected_user_id=user_id, api_key=stripe_key,
        ),
        "media_storage": media.GCSMediaProvider(
            reader, bucket=bucket, snapshot=media_snapshot, freeze=None,
            retention=retention, deferred_cleanup=True,
        ),
        "email_logs_backups": EmailLogsBackupsRetentionProvider(
            request_id=request_id, email_backend=settings.email_provider,
        ),
        "analytics": analytics.BigQueryAnalyticsProvider(
            token=token, project=ANALYTICS_PROJECT_ID, dataset=ANALYTICS_DATASET_ID,
            snapshot=analytics_snapshot, freeze=analytics_freeze,
            reviewed_snapshot_digest=analytics_snapshot.snapshot_digest,
            deferred_cleanup=True,
        ),
        "firebase_auth": FirebaseProvider(app=app),
    }
    if set(providers) != set(PROVIDER_STEPS):
        raise ValueError("provider_plan_invalid")
    return providers, app

