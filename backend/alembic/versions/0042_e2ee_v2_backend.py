"""E2EE v2 backend: versioned device directory, fallback keys, per-device message legs (c444).

RE-POINTED Oct 7: down_revision is 0041. This was written against 0039 while 0040 (c438)
and 0041 (c436) were still on unmerged branches; both landed on main first, so it now
chains after 0041 (`alembic heads` shows the single head 0042).

WHY A NEW SHAPE AND NOT A REINTERPRETATION OF THE OLD COLUMNS (E2EE-DESIGN.md section 4).
The v1 tables are Signal-shaped: one DH identity, a signed-prekey slot, Kyber columns, and
a registration id. Olm needs a second (Ed25519) identity, signed one-time AND fallback
keys, and a generation counter. Everything below is ADDITIVE and NULL-able:

  * LEGACY ROWS ARE LEFT EXACTLY AS THEY ARE. crypto_suite IS NULL marks a legacy device;
    the v2 API refuses it and the v1 API never sees a v2 device's keys. Nothing is
    backfilled, nothing is rewritten. (c442 expected the legacy tables to be empty and
    to fail loudly otherwise; leaving legacy rows untouched makes that unnecessary and
    safe on any database, populated or not.)
  * CHECK constraints make the two states exclusive. A v2 device row must carry its
    Ed25519 identity (32 bytes), a generation (1..2^32-1) and a binding signature (64
    bytes); a legacy row must carry none of the v2 fields and must still have a
    registration_id, which is why that column can become nullable without loosening v1.
  * registration_id becomes NULL-able because Olm has no registration id; the CHECK
    keeps it mandatory for legacy rows.
  * generation is BIGINT, not INT: the client-chosen generation is a u32 (it is signed as
    4 bytes) and a u32 does not fit PostgreSQL's signed INTEGER.
  * approved_at is NOT in the c442 column list and is required: pending and root devices
    both have approved_by_device_id NULL, so without an explicit marker they are
    indistinguishable. Pending = approved_at NULL. Root (the first device of an account,
    or the first device after every earlier one was revoked) = approved_at set,
    approved_by_device_id NULL. Approved by another device = both set, plus the
    approver's signature.
  * one_time_prekeys.key_id widens to BIGINT (vodozemac key ids are u64; the API keeps
    them below 2^63). Two partial unique indexes carry the v2 invariants and ignore
    legacy rows, whose key ids were never constrained: a key id is never reused within a
    device, and a device has at most ONE fallback key (replacing it deletes the old row).
  * messages.ciphertext becomes NULL-able: a v2 message carries no message-level
    ciphertext, only per-device rows in message_legs. Three CHECKs keep the two kinds
    from mixing (legacy rows need ciphertext; v2 rows need a client_message_id and must
    not carry ciphertext). UNIQUE (sender_device_id, client_message_id) is the
    idempotency key.

DOWNGRADE REFUSES RATHER THAN DESTROYS. Dropping v2 columns would silently discard
identities, keys and every v2 message, and narrowing key_id back to INTEGER would fail
midway on a u64 id. If any v2 data exists the downgrade raises before touching anything;
with only legacy data it round-trips with no loss.
"""
from alembic import op
from sqlalchemy import text

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- devices ----
    op.execute(
        """
        ALTER TABLE devices
            ADD COLUMN crypto_suite TEXT,
            ADD COLUMN identity_ed25519 BYTEA,
            ADD COLUMN generation BIGINT,
            ADD COLUMN binding_signature BYTEA,
            ADD COLUMN approved_at TIMESTAMPTZ,
            ADD COLUMN approved_by_device_id UUID REFERENCES devices(id),
            ADD COLUMN approval_signature BYTEA
        """
    )
    # Olm has no registration id; legacy rows keep theirs (see ck_devices_registration_id).
    op.execute("ALTER TABLE devices ALTER COLUMN registration_id DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE devices
            ADD CONSTRAINT ck_devices_v2_shape CHECK (
                crypto_suite IS NULL OR (
                    length(crypto_suite) BETWEEN 1 AND 64
                    AND identity_ed25519 IS NOT NULL AND length(identity_ed25519) = 32
                    AND length(identity_key) = 32
                    AND generation IS NOT NULL AND generation BETWEEN 1 AND 4294967295
                    AND binding_signature IS NOT NULL AND length(binding_signature) = 64
                )
            ),
            ADD CONSTRAINT ck_devices_legacy_has_no_v2_fields CHECK (
                crypto_suite IS NOT NULL OR (
                    identity_ed25519 IS NULL AND generation IS NULL
                    AND binding_signature IS NULL AND approved_at IS NULL
                    AND approved_by_device_id IS NULL AND approval_signature IS NULL
                )
            ),
            ADD CONSTRAINT ck_devices_registration_id CHECK (
                crypto_suite IS NOT NULL OR registration_id IS NOT NULL
            ),
            ADD CONSTRAINT ck_devices_approval_shape CHECK (
                (approved_by_device_id IS NULL AND approval_signature IS NULL)
                OR (
                    approved_by_device_id IS NOT NULL
                    AND approval_signature IS NOT NULL AND length(approval_signature) = 64
                    AND approved_at IS NOT NULL
                    AND approved_by_device_id <> id
                )
            )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_devices_v2_user_generation "
        "ON devices (user_id, generation) WHERE crypto_suite IS NOT NULL"
    )
    # A new identity is a new device (contract section 2): the same public identity can
    # never appear on two device rows, for the same account or any other.
    op.execute(
        "CREATE UNIQUE INDEX uq_devices_v2_identity_curve25519 "
        "ON devices (identity_key) WHERE crypto_suite IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_devices_v2_identity_ed25519 "
        "ON devices (identity_ed25519) WHERE crypto_suite IS NOT NULL"
    )

    # ---- one_time_prekeys (also holds v2 fallback keys, kind = 'fallback') ----
    op.execute("ALTER TABLE one_time_prekeys ALTER COLUMN key_id TYPE BIGINT")
    op.execute(
        """
        ALTER TABLE one_time_prekeys
            ADD COLUMN kind TEXT NOT NULL DEFAULT 'one_time',
            ADD COLUMN signature BYTEA,
            ADD CONSTRAINT ck_otk_kind CHECK (kind IN ('one_time', 'fallback')),
            ADD CONSTRAINT ck_otk_v2_shape CHECK (
                signature IS NULL OR (
                    length(signature) = 64 AND length(public_key) = 32 AND key_id >= 0
                )
            ),
            ADD CONSTRAINT ck_otk_fallback_signed CHECK (
                kind = 'one_time' OR signature IS NOT NULL
            )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_otk_v2_device_key_id "
        "ON one_time_prekeys (device_id, key_id) WHERE signature IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_otk_v2_one_fallback_per_device "
        "ON one_time_prekeys (device_id) WHERE kind = 'fallback'"
    )

    # ---- messages ----
    op.execute("ALTER TABLE messages ALTER COLUMN ciphertext DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE messages
            ADD COLUMN client_message_id UUID,
            ADD COLUMN envelope_version SMALLINT,
            ADD CONSTRAINT ck_messages_ciphertext_or_envelope CHECK (
                envelope_version IS NOT NULL OR ciphertext IS NOT NULL
            ),
            ADD CONSTRAINT ck_messages_v2_no_ciphertext CHECK (
                envelope_version IS NULL OR ciphertext IS NULL
            ),
            ADD CONSTRAINT ck_messages_v2_client_message_id CHECK (
                envelope_version IS NULL OR client_message_id IS NOT NULL
            )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_messages_sender_device_client_message_id "
        "ON messages (sender_device_id, client_message_id) "
        "WHERE client_message_id IS NOT NULL"
    )

    # ---- message_legs ----
    op.execute(
        """
        CREATE TABLE message_legs (
            message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            recipient_device_id UUID NOT NULL REFERENCES devices(id),
            olm_type SMALLINT NOT NULL,
            ciphertext BYTEA NOT NULL,
            PRIMARY KEY (message_id, recipient_device_id),
            CONSTRAINT ck_message_legs_olm_type CHECK (olm_type IN (0, 1)),
            CONSTRAINT ck_message_legs_ciphertext CHECK (length(ciphertext) > 0)
        )
        """
    )
    # Leading recipient_device_id: GET /v2/conversations/{id}/messages reads "the legs
    # for MY device", then joins to messages by id.
    op.execute(
        "CREATE INDEX idx_message_legs_recipient "
        "ON message_legs (recipient_device_id, message_id)"
    )


def downgrade() -> None:
    v2_rows = op.get_bind().execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM devices WHERE crypto_suite IS NOT NULL) AS devices,
                (SELECT count(*) FROM one_time_prekeys
                    WHERE signature IS NOT NULL OR kind <> 'one_time') AS keys,
                (SELECT count(*) FROM messages
                    WHERE envelope_version IS NOT NULL
                       OR client_message_id IS NOT NULL) AS messages,
                (SELECT count(*) FROM message_legs) AS legs
            """
        )
    ).one()
    if any(v2_rows):
        raise RuntimeError(
            "0042 downgrade refused: v2 E2EE data exists "
            f"(devices={v2_rows.devices}, keys={v2_rows.keys}, "
            f"messages={v2_rows.messages}, legs={v2_rows.legs}). Downgrading would "
            "silently discard device identities, keys and encrypted messages. Delete "
            "them deliberately first if that is really intended."
        )

    op.execute("DROP INDEX IF EXISTS idx_message_legs_recipient")
    op.execute("DROP TABLE IF EXISTS message_legs")

    op.execute("DROP INDEX IF EXISTS uq_messages_sender_device_client_message_id")
    op.execute(
        """
        ALTER TABLE messages
            DROP CONSTRAINT IF EXISTS ck_messages_v2_client_message_id,
            DROP CONSTRAINT IF EXISTS ck_messages_v2_no_ciphertext,
            DROP CONSTRAINT IF EXISTS ck_messages_ciphertext_or_envelope,
            DROP COLUMN IF EXISTS envelope_version,
            DROP COLUMN IF EXISTS client_message_id
        """
    )
    op.execute("ALTER TABLE messages ALTER COLUMN ciphertext SET NOT NULL")

    op.execute("DROP INDEX IF EXISTS uq_otk_v2_one_fallback_per_device")
    op.execute("DROP INDEX IF EXISTS uq_otk_v2_device_key_id")
    op.execute(
        """
        ALTER TABLE one_time_prekeys
            DROP CONSTRAINT IF EXISTS ck_otk_fallback_signed,
            DROP CONSTRAINT IF EXISTS ck_otk_v2_shape,
            DROP CONSTRAINT IF EXISTS ck_otk_kind,
            DROP COLUMN IF EXISTS signature,
            DROP COLUMN IF EXISTS kind
        """
    )
    op.execute("ALTER TABLE one_time_prekeys ALTER COLUMN key_id TYPE INTEGER")

    op.execute("DROP INDEX IF EXISTS uq_devices_v2_identity_ed25519")
    op.execute("DROP INDEX IF EXISTS uq_devices_v2_identity_curve25519")
    op.execute("DROP INDEX IF EXISTS uq_devices_v2_user_generation")
    op.execute(
        """
        ALTER TABLE devices
            DROP CONSTRAINT IF EXISTS ck_devices_approval_shape,
            DROP CONSTRAINT IF EXISTS ck_devices_registration_id,
            DROP CONSTRAINT IF EXISTS ck_devices_legacy_has_no_v2_fields,
            DROP CONSTRAINT IF EXISTS ck_devices_v2_shape,
            DROP COLUMN IF EXISTS approval_signature,
            DROP COLUMN IF EXISTS approved_by_device_id,
            DROP COLUMN IF EXISTS approved_at,
            DROP COLUMN IF EXISTS binding_signature,
            DROP COLUMN IF EXISTS generation,
            DROP COLUMN IF EXISTS identity_ed25519,
            DROP COLUMN IF EXISTS crypto_suite
        """
    )
    op.execute("ALTER TABLE devices ALTER COLUMN registration_id SET NOT NULL")
