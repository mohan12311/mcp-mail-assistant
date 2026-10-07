"""0001: original persistent queue schema."""

from db.migrations import Migration, index, table


MIGRATION = Migration(
    version=1,
    name="initial_schema",
    operations=(
        table(
            "processing_state",
            """
            CREATE TABLE processing_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        table(
            "emails",
            """
            CREATE TABLE emails (
                id TEXT PRIMARY KEY,
                message_id TEXT UNIQUE,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                received_at TIMESTAMP NOT NULL,
                body_text TEXT,
                body_summary TEXT,
                priority_score INTEGER DEFAULT 0,
                priority_level TEXT DEFAULT 'low',
                urgency TEXT DEFAULT 'normal',
                has_attachments BOOLEAN DEFAULT 0,
                requires_response BOOLEAN DEFAULT 0,
                is_processed BOOLEAN DEFAULT 0,
                slack_notified BOOLEAN DEFAULT 0,
                slack_ts TEXT,
                folder TEXT DEFAULT 'INBOX',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        table(
            "tasks",
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT NOT NULL REFERENCES emails(id),
                task_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                deadline DATE,
                priority TEXT DEFAULT 'medium',
                status TEXT DEFAULT 'pending',
                approval_status TEXT DEFAULT 'none',
                approval_slack_ts TEXT,
                approved_at TIMESTAMP,
                executed_at TIMESTAMP,
                execution_result TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        table(
            "attachments",
            """
            CREATE TABLE attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT NOT NULL REFERENCES emails(id),
                filename TEXT NOT NULL,
                file_type TEXT,
                file_size INTEGER,
                local_path TEXT,
                extracted_text TEXT,
                key_points TEXT,
                is_processed BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        table(
            "events",
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                lease_owner TEXT,
                lease_until TIMESTAMP,
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                last_error TEXT,
                source_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        index("idx_emails_received", "CREATE INDEX idx_emails_received ON emails(received_at DESC)"),
        index("idx_emails_processed", "CREATE INDEX idx_emails_processed ON emails(is_processed)"),
        index("idx_tasks_status", "CREATE INDEX idx_tasks_status ON tasks(status)"),
        index("idx_tasks_approval", "CREATE INDEX idx_tasks_approval ON tasks(approval_status)"),
        index("idx_attachments_email", "CREATE INDEX idx_attachments_email ON attachments(email_id)"),
        index("idx_events_status_created", "CREATE INDEX idx_events_status_created ON events(status, created_at)"),
        index("idx_events_lease_until", "CREATE INDEX idx_events_lease_until ON events(lease_until)"),
        index("idx_events_status", "CREATE INDEX idx_events_status ON events(status)"),
        index("idx_events_type", "CREATE INDEX idx_events_type ON events(event_type)"),
        index("idx_events_lease", "CREATE INDEX idx_events_lease ON events(status, lease_until)"),
        index("idx_events_source", "CREATE INDEX idx_events_source ON events(source_id)"),
    ),
)
