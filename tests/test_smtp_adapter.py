"""Unit tests for core/smtp_client.py — SMTPAdapter."""
from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from core.smtp_client import SMTPAdapter, SMTPNotConfiguredError


def _make_config(
    smtp_host: str = "mail.example.com",
    smtp_port: int = 587,
    smtp_user: str = "sender@example.com",
    smtp_pass: str = "secret",
    smtp_tls: bool = True,
    smtp_timeout_seconds: int = 30,
) -> MagicMock:
    cfg = MagicMock()
    cfg.smtp_host = smtp_host
    cfg.smtp_port = smtp_port
    cfg.smtp_user = smtp_user
    cfg.smtp_pass = smtp_pass
    cfg.smtp_tls = smtp_tls
    cfg.smtp_timeout_seconds = smtp_timeout_seconds
    return cfg


class TestSMTPNotConfigured:
    def test_raises_when_smtp_host_empty(self) -> None:
        """SMTPNotConfiguredError must be raised before any network call."""
        cfg = _make_config(smtp_host="")
        adapter = SMTPAdapter(cfg)

        with patch("smtplib.SMTP") as mock_smtp, patch("smtplib.SMTP_SSL") as mock_ssl:
            with pytest.raises(SMTPNotConfiguredError, match="SMTP_HOST"):
                adapter.send("to@example.com", "Subject", "Body")

        mock_smtp.assert_not_called()
        mock_ssl.assert_not_called()


class TestSMTPTLS:
    def test_uses_smtp_with_starttls_when_tls_true(self) -> None:
        """smtp_tls=True must use smtplib.SMTP + ehlo() + starttls()."""
        cfg = _make_config(smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        tls_context = MagicMock()
        with patch("core.smtp_client.ssl.create_default_context", return_value=tls_context) as create_context:
            with patch("smtplib.SMTP", return_value=mock_server) as mock_smtp_cls:
                with patch("smtplib.SMTP_SSL") as mock_ssl:
                    adapter.send("to@example.com", "Hello", "World")

        create_context.assert_called_once_with()
        mock_smtp_cls.assert_called_once_with("mail.example.com", 587, timeout=30)
        mock_server.ehlo.assert_called_once()
        mock_server.starttls.assert_called_once_with(context=tls_context)
        mock_ssl.assert_not_called()

    def test_starttls_uses_configured_timeout(self) -> None:
        cfg = _make_config(smtp_tls=True, smtp_timeout_seconds=12)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server) as mock_smtp_cls:
            adapter.send("to@example.com", "Hello", "World")

        mock_smtp_cls.assert_called_once_with("mail.example.com", 587, timeout=12)

    def test_uses_smtp_ssl_when_tls_false(self) -> None:
        """smtp_tls=False must use smtplib.SMTP_SSL (no starttls)."""
        cfg = _make_config(smtp_tls=False, smtp_port=465)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        tls_context = MagicMock()
        with patch("core.smtp_client.ssl.create_default_context", return_value=tls_context) as create_context:
            with patch("smtplib.SMTP_SSL", return_value=mock_server) as mock_ssl_cls:
                with patch("smtplib.SMTP") as mock_smtp:
                    adapter.send("to@example.com", "Hello", "World")

        create_context.assert_called_once_with()
        mock_ssl_cls.assert_called_once_with(
            "mail.example.com",
            465,
            timeout=30,
            context=tls_context,
        )
        mock_server.starttls.assert_not_called()
        mock_smtp.assert_not_called()


class TestSMTPSendmail:
    def test_sendmail_called_with_correct_args(self) -> None:
        """send() must call server.sendmail with from, [to], and message string."""
        cfg = _make_config(smtp_user="from@example.com", smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server):
            adapter.send("to@example.com", "Test Subject", "Test Body")

        args = mock_server.sendmail.call_args
        assert args[0][0] == "from@example.com"   # from
        assert args[0][1] == ["to@example.com"]   # to list
        msg_str: str = args[0][2]
        assert "Test Subject" in msg_str
        # Body is base64-encoded by MIMEText for utf-8; decode to verify content
        import base64
        import email
        parsed = email.message_from_string(msg_str)
        body = base64.b64decode(parsed.get_payload()).decode("utf-8")
        assert "Test Body" in body

    def test_login_called_when_credentials_set(self) -> None:
        """server.login must be called when smtp_user and smtp_pass are both set."""
        cfg = _make_config(smtp_user="u@example.com", smtp_pass="pw", smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server):
            adapter.send("to@example.com", "S", "B")

        mock_server.login.assert_called_once_with("u@example.com", "pw")

    def test_login_skipped_when_no_credentials(self) -> None:
        """server.login must NOT be called when smtp_user or smtp_pass is empty."""
        cfg = _make_config(smtp_user="", smtp_pass="", smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server):
            adapter.send("to@example.com", "S", "B")

        mock_server.login.assert_not_called()

    def test_quit_called_on_success(self) -> None:
        """server.quit() must be called even on normal exit (finally block)."""
        cfg = _make_config(smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server):
            adapter.send("to@example.com", "S", "B")

        mock_server.quit.assert_called_once()

    def test_quit_called_on_sendmail_exception(self) -> None:
        """server.quit() must be called even when sendmail raises."""
        cfg = _make_config(smtp_tls=True)
        adapter = SMTPAdapter(cfg)

        mock_server = MagicMock()
        mock_server.sendmail.side_effect = smtplib.SMTPException("fail")
        with patch("smtplib.SMTP", return_value=mock_server):
            with pytest.raises(smtplib.SMTPException):
                adapter.send("to@example.com", "S", "B")

        mock_server.quit.assert_called_once()
