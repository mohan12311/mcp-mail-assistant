"""0002: POP identity and local reply draft state."""

from db.migrations import Migration, column, index


MIGRATION = Migration(
    version=2,
    name="mail_transport_and_reply_drafts",
    operations=(
        column("emails", "uidl", "TEXT"),
        column("tasks", "send_state", "TEXT"),
        column("tasks", "send_payload", "TEXT"),
        index("idx_emails_uidl", "CREATE INDEX idx_emails_uidl ON emails(uidl)"),
    ),
)
