"""0008: durable Slack conversation requests and task snoozing."""

from db.migrations import Migration, column, index, table


MIGRATION = Migration(
    version=8,
    name="slack_conversation_state",
    operations=(
        column("tasks", "snoozed_until", "TEXT"),
        column("tasks", "last_slack_interaction_id", "TEXT"),
        table(
            "slack_interactions",
            """
            CREATE TABLE slack_interactions (
                id TEXT PRIMARY KEY,
                slack_event_id TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                request_text TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'processed', 'expired', 'failed')
                ),
                response_text TEXT,
                response_kind TEXT CHECK (
                    response_kind IS NULL OR
                    response_kind IN ('message', 'draft_with_confirmation')
                ),
                response_task_id INTEGER REFERENCES tasks(id),
                expires_at TEXT NOT NULL,
                processed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
        table(
            "slack_conversation_contexts",
            """
            CREATE TABLE slack_conversation_contexts (
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                active_task_id INTEGER REFERENCES tasks(id),
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(channel_id, thread_ts)
            )
            """,
        ),
        index(
            "idx_tasks_snoozed_until",
            "CREATE INDEX idx_tasks_snoozed_until ON tasks(status, snoozed_until)",
        ),
        index(
            "uq_tasks_last_slack_interaction",
            """
            CREATE UNIQUE INDEX uq_tasks_last_slack_interaction
            ON tasks(last_slack_interaction_id)
            WHERE last_slack_interaction_id IS NOT NULL
            """,
        ),
        index(
            "idx_slack_interactions_status",
            "CREATE INDEX idx_slack_interactions_status ON slack_interactions(status, expires_at)",
        ),
        index(
            "idx_slack_context_expiry",
            "CREATE INDEX idx_slack_context_expiry ON slack_conversation_contexts(expires_at)",
        ),
    ),
)
