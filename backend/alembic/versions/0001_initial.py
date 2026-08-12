"""Initial schema: full SPEC §3 tables plus the ledger_append_only trigger."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all SPEC §3 tables in dependency order, then the append-only ledger trigger."""
    # ============ IDENTITY & ORGS ============
    # campuses first: users.campus_id references campuses(id).
    op.execute(
        """
        CREATE TABLE campuses (
            id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name    TEXT NOT NULL,
            slug    TEXT UNIQUE NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE users (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firebase_uid    TEXT UNIQUE NOT NULL,
            email           TEXT UNIQUE NOT NULL,
            display_name    TEXT NOT NULL,
            avatar_url      TEXT,
            account_type    TEXT NOT NULL CHECK (account_type IN ('greek', 'non_greek', 'alumni')),
            campus_id       UUID REFERENCES campuses(id),
            is_ghost        BOOLEAN NOT NULL DEFAULT FALSE, -- placeholder members for historical lineage
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE chapters (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campus_id       UUID NOT NULL REFERENCES campuses(id),
            org_name        TEXT NOT NULL,        -- e.g. "Sigma Chi"
            chapter_name    TEXT,                 -- e.g. "Epsilon Mu"
            stripe_account_id TEXT,               -- Stripe Connect connected account
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memberships (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES users(id),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            role        TEXT NOT NULL CHECK (role IN
                        ('president','vice_president','treasurer','secretary',
                         'historian','member','pledge','alumni')),
            status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','removed')),
            pledge_class TEXT,                    -- e.g. "Fall 2024"
            joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, chapter_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_memberships_chapter ON memberships(chapter_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX idx_memberships_user ON memberships(user_id) WHERE status = 'active'"
    )
    op.execute(
        """
        CREATE TABLE chapter_invites (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            code        TEXT UNIQUE NOT NULL,     -- deep-link invite code
            role        TEXT NOT NULL DEFAULT 'member',
            expires_at  TIMESTAMPTZ,
            created_by  UUID NOT NULL REFERENCES users(id)
        )
        """
    )

    # ============ E2EE KEY DIRECTORY ============
    op.execute(
        """
        CREATE TABLE devices (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL REFERENCES users(id),
            device_label    TEXT,
            registration_id INTEGER NOT NULL,     -- libsignal registration id
            identity_key    BYTEA NOT NULL,       -- public identity key
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at      TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE signed_prekeys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id   UUID NOT NULL REFERENCES devices(id),
            key_id      INTEGER NOT NULL,
            public_key  BYTEA NOT NULL,
            signature   BYTEA NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE one_time_prekeys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id   UUID NOT NULL REFERENCES devices(id),
            key_id      INTEGER NOT NULL,
            public_key  BYTEA NOT NULL,
            consumed_at TIMESTAMPTZ               -- server hands out once, marks consumed
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_otk_available ON one_time_prekeys(device_id) WHERE consumed_at IS NULL"
    )

    # ============ MESSAGING (ciphertext only) ============
    op.execute(
        """
        CREATE TABLE conversations (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID REFERENCES chapters(id),  -- NULL for cross-chapter DMs
            kind        TEXT NOT NULL CHECK (kind IN ('dm','group')),
            title       TEXT,                          -- group name (plaintext metadata, OK)
            protocol_version INTEGER NOT NULL DEFAULT 2, -- 2 = E2EE from day one
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE conversation_members (
            conversation_id UUID NOT NULL REFERENCES conversations(id),
            user_id         UUID NOT NULL REFERENCES users(id),
            joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            left_at         TIMESTAMPTZ,               -- triggers sender-key rotation client-side
            PRIMARY KEY (conversation_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE messages (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL REFERENCES conversations(id),
            sender_device_id UUID NOT NULL REFERENCES devices(id),
            ciphertext      BYTEA NOT NULL,            -- server NEVER parses this
            message_type    TEXT NOT NULL DEFAULT 'signal', -- signal | sender_key_distribution
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_messages_convo_time ON messages(conversation_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE message_receipts (
            message_id  UUID NOT NULL REFERENCES messages(id),
            device_id   UUID NOT NULL REFERENCES devices(id),
            delivered_at TIMESTAMPTZ,
            PRIMARY KEY (message_id, device_id)
        )
        """
    )

    # ============ SOCIAL FEED ============
    op.execute(
        """
        CREATE TABLE posts (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            author_id   UUID NOT NULL REFERENCES users(id),
            body        TEXT NOT NULL,
            media_urls  TEXT[],
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at  TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_posts_chapter_time ON posts(chapter_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE post_likes (
            post_id UUID NOT NULL REFERENCES posts(id),
            user_id UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (post_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE post_comments (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            post_id     UUID NOT NULL REFERENCES posts(id),
            author_id   UUID NOT NULL REFERENCES users(id),
            body        TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at  TIMESTAMPTZ
        )
        """
    )

    # ============ YAK (anonymous board) ============
    op.execute(
        """
        CREATE TABLE yaks (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campus_id   UUID NOT NULL REFERENCES campuses(id),
            author_id   UUID NOT NULL REFERENCES users(id),  -- NEVER exposed via API
            body        TEXT NOT NULL,
            score       INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            removed_at  TIMESTAMPTZ,
            removed_reason TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_yaks_campus_time ON yaks(campus_id, created_at DESC) WHERE removed_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE yak_votes (
            yak_id  UUID NOT NULL REFERENCES yaks(id),
            user_id UUID NOT NULL REFERENCES users(id),
            value   SMALLINT NOT NULL CHECK (value IN (-1, 1)),
            PRIMARY KEY (yak_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE content_reports (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reporter_id     UUID NOT NULL REFERENCES users(id),
            target_type     TEXT NOT NULL CHECK (target_type IN ('yak','post','comment','message_forward','user')),
            target_id       UUID,
            forwarded_plaintext TEXT,      -- for E2EE message reports: client forwards plaintext
            reason          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','actioned','dismissed')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE user_blocks (
            blocker_id UUID NOT NULL REFERENCES users(id),
            blocked_id UUID NOT NULL REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (blocker_id, blocked_id)
        )
        """
    )

    # ============ LINEAGE (family tree) ============
    op.execute(
        """
        CREATE TABLE families (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            name        TEXT NOT NULL,      -- "Hammer family"
            color       TEXT NOT NULL DEFAULT '#6366f1'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE lineage_edges (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id      UUID NOT NULL REFERENCES chapters(id),
            big_user_id     UUID NOT NULL REFERENCES users(id),
            little_user_id  UUID NOT NULL REFERENCES users(id),
            family_id       UUID REFERENCES families(id),
            pledge_class    TEXT,
            confirmed_by_little BOOLEAN NOT NULL DEFAULT FALSE,
            created_by      UUID NOT NULL REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (little_user_id, chapter_id)  -- one big per member per chapter
        )
        """
    )
    op.execute("CREATE INDEX idx_lineage_chapter ON lineage_edges(chapter_id)")

    # ============ FINANCE (append-only) ============
    op.execute(
        """
        CREATE TABLE dues_cycles (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            name        TEXT NOT NULL,          -- "Spring 2027 Dues"
            amount_cents INTEGER NOT NULL,
            due_date    DATE NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ledger_entries (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id      UUID NOT NULL REFERENCES chapters(id),
            entry_type      TEXT NOT NULL CHECK (entry_type IN
                            ('dues_payment','expense','budget_allocation','correction','payout')),
            amount_cents    INTEGER NOT NULL,   -- positive = in, negative = out
            category        TEXT,
            description     TEXT,
            related_user_id UUID REFERENCES users(id),   -- who paid, for dues
            dues_cycle_id   UUID REFERENCES dues_cycles(id),
            stripe_payment_intent_id TEXT,
            corrects_entry_id UUID REFERENCES ledger_entries(id), -- for corrections
            created_by      UUID NOT NULL REFERENCES users(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            -- NO updated_at. NO soft delete. Append-only by design.
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_ledger_chapter_time ON ledger_entries(chapter_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE spend_approvals (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            requested_by UUID NOT NULL REFERENCES users(id),
            amount_cents INTEGER NOT NULL,
            description TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
            decided_by  UUID REFERENCES users(id),
            decided_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # ============ SECRETARY ============
    op.execute(
        """
        CREATE TABLE meetings (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            chapter_id  UUID NOT NULL REFERENCES chapters(id),
            title       TEXT NOT NULL,
            meeting_date TIMESTAMPTZ NOT NULL,
            minutes_md  TEXT,                   -- markdown minutes
            created_by  UUID NOT NULL REFERENCES users(id),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE meeting_attendance (
            meeting_id UUID NOT NULL REFERENCES meetings(id),
            user_id    UUID NOT NULL REFERENCES users(id),
            status     TEXT NOT NULL CHECK (status IN ('present','absent','excused')),
            PRIMARY KEY (meeting_id, user_id)
        )
        """
    )

    # ============ ALUMNI / JOBS ============
    op.execute(
        """
        CREATE TABLE alumni_profiles (
            user_id     UUID PRIMARY KEY REFERENCES users(id),
            grad_year   INTEGER,
            company     TEXT,
            title       TEXT,
            industry    TEXT,
            linkedin_url TEXT,
            open_to_mentoring BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    op.execute(
        """
        CREATE TABLE job_posts (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            posted_by   UUID NOT NULL REFERENCES users(id),
            chapter_id  UUID REFERENCES chapters(id),  -- NULL = network-wide
            title       TEXT NOT NULL,
            company     TEXT NOT NULL,
            description TEXT NOT NULL,
            apply_url   TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ
        )
        """
    )

    # ============ FINANCE DEFENSE-IN-DEPTH (SPEC §8.2 / CONVENTIONS "Finance rules") ============
    op.execute(
        """
        CREATE FUNCTION ledger_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entries is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_append_only
            BEFORE UPDATE OR DELETE ON ledger_entries
            FOR EACH ROW EXECUTE FUNCTION ledger_append_only()
        """
    )


def downgrade() -> None:
    """Drop the ledger trigger + function, then all tables in reverse dependency order."""
    op.execute("DROP TRIGGER IF EXISTS ledger_append_only ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS ledger_append_only()")
    op.execute("DROP TABLE IF EXISTS job_posts")
    op.execute("DROP TABLE IF EXISTS alumni_profiles")
    op.execute("DROP TABLE IF EXISTS meeting_attendance")
    op.execute("DROP TABLE IF EXISTS meetings")
    op.execute("DROP TABLE IF EXISTS spend_approvals")
    op.execute("DROP TABLE IF EXISTS ledger_entries")
    op.execute("DROP TABLE IF EXISTS dues_cycles")
    op.execute("DROP TABLE IF EXISTS lineage_edges")
    op.execute("DROP TABLE IF EXISTS families")
    op.execute("DROP TABLE IF EXISTS user_blocks")
    op.execute("DROP TABLE IF EXISTS content_reports")
    op.execute("DROP TABLE IF EXISTS yak_votes")
    op.execute("DROP TABLE IF EXISTS yaks")
    op.execute("DROP TABLE IF EXISTS post_comments")
    op.execute("DROP TABLE IF EXISTS post_likes")
    op.execute("DROP TABLE IF EXISTS posts")
    op.execute("DROP TABLE IF EXISTS message_receipts")
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversation_members")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS one_time_prekeys")
    op.execute("DROP TABLE IF EXISTS signed_prekeys")
    op.execute("DROP TABLE IF EXISTS devices")
    op.execute("DROP TABLE IF EXISTS chapter_invites")
    op.execute("DROP TABLE IF EXISTS memberships")
    op.execute("DROP TABLE IF EXISTS chapters")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TABLE IF EXISTS campuses")
