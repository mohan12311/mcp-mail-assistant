"""0004: inbound RFC/Outlook reply-thread metadata."""

from db.migrations import Migration, column


MIGRATION = Migration(
    version=4,
    name="reply_thread_metadata",
    operations=(
        column("emails", "in_reply_to", "TEXT"),
        column("emails", "references_header", "TEXT"),
        column("emails", "thread_topic", "TEXT"),
    ),
)
