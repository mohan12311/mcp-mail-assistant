"""0006: event uniqueness, new-email commit/outbox, and retry scheduling."""

from db.migrations import Migration, column, data, index, table


_NO_DUPLICATE_SOURCE_AND_TYPE = """
SELECT NOT EXISTS (
    SELECT 1
    FROM events
    WHERE source_id IS NOT NULL
    GROUP BY source_id, event_type
    HAVING COUNT(*) > 1
)
"""

_NO_DUPLICATE_SOURCE = """
SELECT NOT EXISTS (
    SELECT 1
    FROM events
    WHERE source_id IS NOT NULL
    GROUP BY source_id
    HAVING COUNT(*) > 1
)
"""


MIGRATION = Migration(
    version=6,
    name="event_and_new_email_data_consistency",
    operations=(
        column("events", "next_attempt_at", "TEXT"),
        column("tasks", "idempotency_key", "TEXT"),
        table(
            "event_duplicate_archive",
            """
            CREATE TABLE event_duplicate_archive (
                archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                lease_owner TEXT,
                lease_until TEXT,
                attempts INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                last_error TEXT,
                source_id TEXT,
                next_attempt_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                archive_reason TEXT NOT NULL,
                archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ),
        data(
            "archive_same_intent_event_duplicates",
            """
            INSERT OR IGNORE INTO event_duplicate_archive (
                original_event_id, event_type, payload_json, status,
                lease_owner, lease_until, attempts, max_attempts, last_error,
                source_id, next_attempt_at, created_at, updated_at, archive_reason
            )
            WITH ranked AS (
                SELECT events.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_id, event_type
                           ORDER BY
                               CASE status
                                   WHEN 'done' THEN 0
                                   WHEN 'failed' THEN 1
                                   WHEN 'leased' THEN 2
                                   ELSE 3
                               END,
                               created_at,
                               id
                       ) AS duplicate_rank
                FROM events
                WHERE source_id IS NOT NULL
            )
            SELECT id, event_type, payload_json, status,
                   lease_owner, lease_until, COALESCE(attempts, 0),
                   COALESCE(max_attempts, 3), last_error,
                   source_id, next_attempt_at, created_at, updated_at,
                   'duplicate_source_and_type'
            FROM ranked
            WHERE duplicate_rank > 1
            """,
            check_sql="""
            SELECT NOT EXISTS (
                SELECT 1
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id, event_type
                               ORDER BY
                                   CASE status
                                       WHEN 'done' THEN 0
                                       WHEN 'failed' THEN 1
                                       WHEN 'leased' THEN 2
                                       ELSE 3
                                   END,
                                   created_at,
                                   id
                           ) AS duplicate_rank
                    FROM events
                    WHERE source_id IS NOT NULL
                ) AS ranked
                LEFT JOIN event_duplicate_archive archive
                  ON archive.original_event_id = ranked.id
                WHERE ranked.duplicate_rank > 1
                  AND archive.original_event_id IS NULL
            )
            """,
        ),
        data(
            "remove_same_intent_event_duplicates",
            """
            DELETE FROM events
            WHERE id IN (
                SELECT original_event_id
                FROM event_duplicate_archive
                WHERE archive_reason = 'duplicate_source_and_type'
            )
            """,
            check_sql=_NO_DUPLICATE_SOURCE_AND_TYPE,
        ),
        data(
            "archive_cross_type_source_collisions",
            """
            INSERT OR IGNORE INTO event_duplicate_archive (
                original_event_id, event_type, payload_json, status,
                lease_owner, lease_until, attempts, max_attempts, last_error,
                source_id, next_attempt_at, created_at, updated_at, archive_reason
            )
            WITH ranked AS (
                SELECT events.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_id
                           ORDER BY
                               CASE status
                                   WHEN 'done' THEN 0
                                   WHEN 'failed' THEN 1
                                   WHEN 'leased' THEN 2
                                   ELSE 3
                               END,
                               created_at,
                               id
                       ) AS collision_rank
                FROM events
                WHERE source_id IS NOT NULL
            )
            SELECT id, event_type, payload_json, status,
                   lease_owner, lease_until, COALESCE(attempts, 0),
                   COALESCE(max_attempts, 3), last_error,
                   source_id, next_attempt_at, created_at, updated_at,
                   'cross_type_source_collision_renamed'
            FROM ranked
            WHERE collision_rank > 1
            """,
            check_sql="""
            SELECT NOT EXISTS (
                SELECT 1
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id
                               ORDER BY
                                   CASE status
                                       WHEN 'done' THEN 0
                                       WHEN 'failed' THEN 1
                                       WHEN 'leased' THEN 2
                                       ELSE 3
                                   END,
                                   created_at,
                                   id
                           ) AS collision_rank
                    FROM events
                    WHERE source_id IS NOT NULL
                ) AS ranked
                LEFT JOIN event_duplicate_archive archive
                  ON archive.original_event_id = ranked.id
                WHERE ranked.collision_rank > 1
                  AND archive.original_event_id IS NULL
            )
            """,
        ),
        data(
            "rename_cross_type_source_collisions",
            """
            UPDATE events
            SET source_id = 'legacy:event:' || id || ':source:' || source_id,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN (
                SELECT original_event_id
                FROM event_duplicate_archive
                WHERE archive_reason = 'cross_type_source_collision_renamed'
            )
            """,
            check_sql=_NO_DUPLICATE_SOURCE,
        ),
        table(
            "email_processing_commits",
            """
            CREATE TABLE email_processing_commits (
                email_id TEXT PRIMARY KEY REFERENCES emails(id),
                source_event_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                UNIQUE(source_event_id)
            )
            """,
        ),
        table(
            "operator_outbox",
            """
            CREATE TABLE operator_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN (
                        'pending', 'dispatching', 'dispatched',
                        'discarded', 'delivery_unknown'
                    )
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT,
                lease_owner TEXT,
                lease_until TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
        index(
            "uq_events_source_id",
            """
            CREATE UNIQUE INDEX uq_events_source_id
            ON events(source_id) WHERE source_id IS NOT NULL
            """,
        ),
        index(
            "uq_tasks_idempotency_key",
            """
            CREATE UNIQUE INDEX uq_tasks_idempotency_key
            ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL
            """,
        ),
        index(
            "idx_events_ready",
            """
            CREATE INDEX idx_events_ready
            ON events(status, next_attempt_at, created_at)
            """,
        ),
        index(
            "idx_operator_outbox_ready",
            """
            CREATE INDEX idx_operator_outbox_ready
            ON operator_outbox(status, available_at, created_at)
            """,
        ),
        index(
            "idx_operator_outbox_aggregate",
            """
            CREATE INDEX idx_operator_outbox_aggregate
            ON operator_outbox(aggregate_type, aggregate_id, id)
            """,
        ),
    ),
)
