"""Unit and e2e tests for core/mail_adapter.py — POP3Adapter and factory."""
from __future__ import annotations

import email as _email
import email.message
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    pop_host: str = "pop.example.com",
    pop_port: int = 995,
    pop_user: str = "user@example.com",
    pop_pass: str = "secret",
    pop_ssl: bool = True,
    pop_delete_after_download: bool = False,
) -> MagicMock:
    cfg = MagicMock()
    cfg.pop_host = pop_host
    cfg.pop_port = pop_port
    cfg.pop_user = pop_user
    cfg.pop_pass = pop_pass
    cfg.pop_ssl = pop_ssl
    cfg.pop_delete_after_download = pop_delete_after_download
    return cfg


def _make_raw_message(
    subject: str = "Test Subject",
    sender: str = "from@example.com",
    message_id: str = "<test-001@example.com>",
    date: str = "Mon, 01 Jan 2024 00:00:00 +0000",
    body: str = "Test body",
) -> bytes:
    msg = _email.message.Message()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-ID"] = message_id
    msg["Date"] = date
    msg.set_payload(body)
    return msg.as_bytes()


def _split_to_lines(raw: bytes) -> list[bytes]:
    return raw.split(b"\n")


def _make_mock_conn(
    message_count: int = 1,
    subject: str = "Test Subject",
    sender: str = "from@example.com",
    message_id: str = "<test-001@example.com>",
    uidl_id: str = "unique-id-123",
) -> MagicMock:
    raw = _make_raw_message(subject=subject, sender=sender, message_id=message_id)
    lines = _split_to_lines(raw)
    mock_conn = MagicMock()
    mock_conn.stat.return_value = (message_count, message_count * 512)
    mock_conn.retr.return_value = (b"+OK", lines, len(raw))

    def uidl(*args):
        if args:
            return f"+OK {args[0]} {uidl_id}".encode()
        return b"+OK", [f"1 {uidl_id}".encode()], 0

    mock_conn.uidl.side_effect = uidl
    return mock_conn


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------

class TestCreateMailAdapterFactory:
    @pytest.mark.unit
    def test_returns_pop3_adapter_when_pop_host_set(self) -> None:
        """create_mail_adapter() returns POP3Adapter when pop_host is non-empty."""
        from core.mail_adapter import POP3Adapter, create_mail_adapter

        cfg = _make_config(pop_host="pop.example.com")
        adapter = create_mail_adapter(cfg)
        assert isinstance(adapter, POP3Adapter)

    @pytest.mark.unit
    def test_returns_imap_adapter_when_pop_host_empty(self) -> None:
        """create_mail_adapter() returns IMAPAdapter when pop_host is empty string."""
        from core.mail_adapter import IMAPAdapter, create_mail_adapter

        cfg = _make_config(pop_host="")
        # IMAPClient.__init__ will try to connect; patch it so we don't need real creds
        with patch("core.imap_client.IMAPClient.__init__", return_value=None):
            adapter = create_mail_adapter(cfg)
        assert isinstance(adapter, IMAPAdapter)


# ---------------------------------------------------------------------------
# POP3Adapter unit tests (monkeypatched poplib)
# ---------------------------------------------------------------------------

class TestPOP3AdapterFetchNewEmails:
    @pytest.mark.unit
    def test_subject_sender_date_extracted(self) -> None:
        """fetch_new_emails() correctly extracts subject, sender, and date."""
        mock_conn = _make_mock_conn(
            subject="Hello World",
            sender="alice@example.com",
        )
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            results = adapter.fetch_new_emails(limit=1)

        assert len(results) == 1
        assert results[0].subject == "Hello World"
        assert results[0].sender == "alice@example.com"
        assert results[0].date == "Mon, 01 Jan 2024 00:00:00 +0000"

    @pytest.mark.unit
    def test_uidl_extracted(self) -> None:
        """fetch_new_emails() populates the uidl field from UIDL response."""
        mock_conn = _make_mock_conn(uidl_id="pop-uid-abc")
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            results = adapter.fetch_new_emails(limit=1)

        assert len(results) == 1
        assert results[0].uidl == "pop-uid-abc"

    @pytest.mark.unit
    def test_message_id_extracted(self) -> None:
        """fetch_new_emails() populates message_id from the Message-ID header."""
        mock_conn = _make_mock_conn(message_id="<specific-msg-id@example.com>")
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            results = adapter.fetch_new_emails(limit=1)

        assert len(results) == 1
        assert results[0].message_id == "<specific-msg-id@example.com>"

    @pytest.mark.unit
    def test_no_dele_by_default(self) -> None:
        """conn.dele() must NOT be called when pop_delete_after_download=False."""
        mock_conn = _make_mock_conn()
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config(pop_delete_after_download=False))
            adapter.fetch_new_emails(limit=1)

        mock_conn.dele.assert_not_called()

    @pytest.mark.unit
    def test_dele_not_called_during_fetch_when_delete_after_download(self) -> None:
        """fetch_new_emails() must not delete before body/storage processing succeeds."""
        mock_conn = _make_mock_conn()
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config(pop_delete_after_download=True))
            adapter.fetch_new_emails(limit=1)

        mock_conn.dele.assert_not_called()

    @pytest.mark.unit
    def test_dele_not_called_during_body_fetch_when_delete_after_download(self) -> None:
        """get_email_body() must not delete before DB persistence succeeds."""
        mock_conn = _make_mock_conn()
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config(pop_delete_after_download=True))
            adapter.get_email_body("1")

        mock_conn.dele.assert_not_called()

    @pytest.mark.unit
    def test_delete_email_uses_uidl_to_find_current_message_number(self) -> None:
        """Post-save deletion should find the current POP message number by UIDL."""
        mock_conn = _make_mock_conn(uidl_id="pop-uid-abc")
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            deleted = adapter.delete_email("99", uidl="pop-uid-abc")

        assert deleted is True
        mock_conn.dele.assert_called_once_with(1)

    @pytest.mark.unit
    def test_quit_called_after_fetch(self) -> None:
        """conn.quit() must be called in the finally block after fetch."""
        mock_conn = _make_mock_conn()
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            adapter.fetch_new_emails(limit=1)

        mock_conn.quit.assert_called_once()

    @pytest.mark.unit
    def test_uid_is_string_of_message_number(self) -> None:
        """fetch_new_emails() sets uid to the string representation of the POP3 message number."""
        mock_conn = _make_mock_conn(message_count=1)
        with patch("poplib.POP3_SSL", return_value=mock_conn):
            from core.mail_adapter import POP3Adapter
            adapter = POP3Adapter(_make_config())
            results = adapter.fetch_new_emails(limit=1)

        assert results[0].uid == "1"


class TestIMAPAdapter:
    @pytest.mark.unit
    def test_uses_connection_context_and_preserves_sender_email(self) -> None:
        """IMAPAdapter must keep the existing IMAPClient connection lifecycle and fields."""
        from contextlib import contextmanager
        from datetime import datetime
        from types import SimpleNamespace

        from core.mail_adapter import IMAPAdapter

        class FakeIMAPClient:
            def __init__(self):
                self.connection_enters = 0

            @contextmanager
            def connection(self):
                self.connection_enters += 1
                yield self

            def list_emails(self, limit=10, only_unseen=True):
                return [
                    SimpleNamespace(
                        email_id="imap-1",
                        subject="IMAP Subject",
                        sender="Alice",
                        sender_email="alice@example.com",
                        received_at=datetime(2026, 6, 5, 12, 0, 0),
                    )
                ]

            def read_email(self, uid):
                return SimpleNamespace(
                    body="Body",
                    attachments=[],
                    subject="IMAP Subject",
                    sender="Alice",
                    sender_email="alice@example.com",
                    received_at=datetime(2026, 6, 5, 12, 0, 0),
                    message_id="<imap-1@example.com>",
                )

        fake_client = FakeIMAPClient()
        adapter = IMAPAdapter(fake_client)

        meta = adapter.fetch_new_emails(limit=1)[0]
        content = adapter.get_email_body("imap-1")

        assert fake_client.connection_enters == 2
        assert meta.sender_email == "alice@example.com"
        assert content.sender_email == "alice@example.com"
        assert content.message_id == "<imap-1@example.com>"


class TestPOP3SkillsIntegration:
    @pytest.mark.unit
    def test_list_with_body_preserves_uidl_and_processor_saves_email(self, isolated_db) -> None:
        """POP metadata must survive skills -> watcher processor -> emails table."""
        from skills.mail_skills import init_config, list_emails_with_body
        from watcher.dedupe import DedupeChecker
        from watcher.event_queue import EmailEvent
        from watcher.processor import EventProcessor
        from db import email_store

        mock_conn = _make_mock_conn(
            subject="POP Stored",
            sender="Sender <sender@example.com>",
            message_id="<pop-stored@example.com>",
            uidl_id="UIDL-STORED-1",
        )
        init_config(_make_config(pop_delete_after_download=False))

        with patch("poplib.POP3_SSL", return_value=mock_conn):
            emails = list_emails_with_body(limit=1)

        assert emails[0]["sender_email"] == "sender@example.com"
        assert emails[0]["message_id"] == "<pop-stored@example.com>"
        assert emails[0]["uidl"] == "UIDL-STORED-1"
        assert emails[0]["source_uid"] == "1"
        assert emails[0]["id"].startswith("pop-")
        assert emails[0]["id"] != emails[0]["source_uid"]

        processor = EventProcessor(DedupeChecker())
        assert processor.process_event(EmailEvent("new_email", emails[0]["id"], emails[0])) is True

        stored = email_store.get_email(emails[0]["id"])
        assert stored["sender_email"] == "sender@example.com"
        assert stored["message_id"] == "<pop-stored@example.com>"
        assert stored["uidl"] == "UIDL-STORED-1"

        duplicate = dict(emails[0])
        duplicate["id"] = "different-pop-message-number"
        assert processor.process_event(EmailEvent("new_email", duplicate["id"], duplicate)) is False
        assert email_store.get_email(duplicate["id"]) is None

    @pytest.mark.unit
    def test_processor_deletes_pop_source_only_after_save(self, isolated_db) -> None:
        """delete_after_processed must run after the email is saved locally."""
        from db import email_store
        from watcher.dedupe import DedupeChecker
        from watcher.event_queue import EmailEvent
        from watcher.processor import EventProcessor

        email_data = {
            "id": "pop-delete-after-save",
            "source_uid": "7",
            "message_id": "<pop-delete-after-save@example.com>",
            "uidl": "UIDL-DELETE-AFTER-SAVE",
            "subject": "Delete after save",
            "sender": "Sender",
            "sender_email": "sender@example.com",
            "date": "Fri, 05 Jun 2026 12:00:00 +0900",
            "body": "Body",
            "delete_after_processed": True,
        }

        def fake_delete(email_id: str, uidl: str | None = None) -> bool:
            assert email_id == "7"
            assert email_store.get_email("pop-delete-after-save") is not None
            assert uidl == "UIDL-DELETE-AFTER-SAVE"
            return True

        processor = EventProcessor(DedupeChecker())
        with patch("skills.mail_skills.delete_email_after_processed", side_effect=fake_delete) as mock_delete:
            assert processor.process_event(
                EmailEvent("new_email", email_data["id"], email_data)
            ) is True

        mock_delete.assert_called_once()


# ---------------------------------------------------------------------------
# e2e test (real POP3 server — skipped unless POP_HOST is set)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.skipif(not os.getenv("POP_HOST"), reason="POP_HOST not set")
def test_pop3_real_connection_fetch() -> None:
    """Connect to a real POP3 server and fetch headers (requires env vars)."""
    from core.config import Config
    from core.mail_adapter import POP3Adapter

    config = Config.from_env()
    adapter = POP3Adapter(config)
    results = adapter.fetch_new_emails(limit=5)
    # Simply assert we got a list back (server may have 0 messages)
    assert isinstance(results, list)
    for item in results:
        assert item.uid
        assert isinstance(item.subject, str)
        assert isinstance(item.sender, str)
