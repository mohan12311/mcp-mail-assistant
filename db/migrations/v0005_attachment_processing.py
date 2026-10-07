"""0005: durable attachment validation and extraction state."""

from db.migrations import Migration, column


MIGRATION = Migration(
    version=5,
    name="attachment_processing_metadata",
    operations=(
        column("attachments", "detected_mime", "TEXT"),
        column("attachments", "content_sha256", "TEXT"),
        column(
            "attachments",
            "processing_status",
            "TEXT NOT NULL DEFAULT 'pending'",
        ),
        column("attachments", "processing_error", "TEXT"),
    ),
)
