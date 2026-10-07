"""0003: durable SMTP side-effect ledger and task claim metadata."""

from db.migrations import Migration, column, index, table


MIGRATION = Migration(
    version=3,
    name="outbound_send_ledger",
    operations=(
        column("tasks", "send_claimed_at", "TEXT"),
        column("tasks", "send_claim_event_id", "TEXT"),
        table(
            "outbound_send_ledger",
            """
            CREATE TABLE outbound_send_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                event_id TEXT NOT NULL,
                approval_payload_hash TEXT NOT NULL,
                outbound_message_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('sending', 'sent', 'send_failed', 'delivery_unknown')
                ),
                claimed_at TEXT NOT NULL,
                smtp_completed_at TEXT,
                finalized_at TEXT,
                last_error TEXT,
                uncertainty_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id)
            )
            """,
        ),
        index(
            "idx_outbound_send_task",
            "CREATE INDEX idx_outbound_send_task ON outbound_send_ledger(task_id, created_at DESC)",
        ),
        index(
            "idx_outbound_send_status",
            "CREATE INDEX idx_outbound_send_status ON outbound_send_ledger(status, updated_at)",
        ),
        index(
            "idx_outbound_send_one_sending_per_task",
            """
            CREATE UNIQUE INDEX idx_outbound_send_one_sending_per_task
            ON outbound_send_ledger(task_id) WHERE status = 'sending'
            """,
        ),
    ),
)
