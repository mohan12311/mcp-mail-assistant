"""0009: versioned, user-scoped email handling preferences."""

from db.migrations import Migration, index, table


MIGRATION = Migration(
    version=9,
    name="user_email_preferences",
    operations=(
        table(
            "user_preferences",
            """
            CREATE TABLE user_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                mailbox_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                skill_name TEXT NOT NULL CHECK (
                    skill_name IN ('email_triage', 'reply_drafting')
                ),
                rule_kind TEXT NOT NULL CHECK (
                    rule_kind IN ('processing_exclusion', 'reply_style')
                ),
                scope_type TEXT NOT NULL CHECK (
                    scope_type IN (
                        'default', 'sender_email', 'sender_domain',
                        'sender_name', 'subject_contains'
                    )
                ),
                scope_value TEXT NOT NULL,
                instruction TEXT,
                effects_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'active', 'disabled', 'expired')
                ),
                source_interaction_id TEXT NOT NULL UNIQUE,
                confirmation_interaction_id TEXT UNIQUE,
                disabled_by_interaction_id TEXT,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                expires_at TEXT,
                confirmed_at TEXT,
                disabled_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
        index(
            "idx_user_preferences_resolution",
            """
            CREATE INDEX idx_user_preferences_resolution
            ON user_preferences (
                tenant_id, mailbox_id, user_id, status, rule_kind, scope_type
            )
            """,
        ),
        index(
            "idx_user_preferences_pending_context",
            """
            CREATE INDEX idx_user_preferences_pending_context
            ON user_preferences (
                user_id, channel_id, thread_ts, status, expires_at
            )
            """,
        ),
        index(
            "uq_user_preferences_active_scope",
            """
            CREATE UNIQUE INDEX uq_user_preferences_active_scope
            ON user_preferences (
                tenant_id, mailbox_id, user_id, rule_kind,
                scope_type, scope_value
            )
            WHERE status = 'active'
            """,
        ),
    ),
)
