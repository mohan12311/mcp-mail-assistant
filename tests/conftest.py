import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Provide an initialized SQLite DB isolated from the repo-local db file."""
    db_path = tmp_path / "mail_agent.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    from db import initialize

    initialize()
    return db_path


@pytest.fixture
def sample_email(isolated_db):
    from db import email_store

    email = {
        "id": "email-test-1",
        "message_id": "<email-test-1@example.com>",
        "subject": "Approval test",
        "sender": "Tester",
        "sender_email": "tester@example.com",
        "received_at": datetime.now().isoformat(),
        "body_text": "Please approve this task.",
        "folder": "INBOX",
    }
    email_store.save_email(email)
    return email


@pytest.fixture
def sample_task(sample_email):
    from db import task_store

    task_id = task_store.create_task(
        email_id=sample_email["id"],
        task_type="reply_email",
        title="Reply to approval test",
        description="Draft a reply",
        priority="medium",
        approval_status="pending",
    )
    return task_id
