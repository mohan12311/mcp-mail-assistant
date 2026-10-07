"""0007: expiring, version-bound approval authority."""

from db.migrations import Migration, column, index


MIGRATION = Migration(
    version=7,
    name="approval_authority_and_expiry",
    operations=(
        column("tasks", "approval_requested_at", "TEXT"),
        column("tasks", "approval_expires_at", "TEXT"),
        column("tasks", "approval_payload_sha256", "TEXT"),
        column("tasks", "approval_source", "TEXT"),
        column("tasks", "approval_version", "INTEGER NOT NULL DEFAULT 0"),
        column("events", "expires_at", "TEXT"),
        index(
            "idx_tasks_approval_expiry",
            "CREATE INDEX idx_tasks_approval_expiry ON tasks(approval_status, approval_expires_at)",
        ),
        index(
            "idx_events_expiry",
            "CREATE INDEX idx_events_expiry ON events(event_type, expires_at)",
        ),
    ),
)
