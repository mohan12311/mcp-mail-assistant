"""SMTP send adapter for MCP Mail Assistant email send workflow."""
from __future__ import annotations

import smtplib
import ssl
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Optional

from core.email_headers import (
    generate_message_id,
    normalize_message_id,
    normalize_references,
    normalize_thread_topic,
    optional_message_id,
    validate_header_value,
    validate_mailbox,
)

if TYPE_CHECKING:
    from core.config import Config


class SMTPNotConfiguredError(Exception):
    """Raised when smtp_host is empty — before any network call."""


class SMTPSendRejectedError(smtplib.SMTPException):
    """SMTP definitively rejected the message before accepting delivery."""


class SMTPDeliveryUnknownError(smtplib.SMTPException):
    """The connection failed while SMTP acceptance may already have occurred."""


class SMTPAdapter:
    """Sends email via SMTP. Raises SMTPNotConfiguredError if smtp_host unset."""

    def __init__(self, config: "Config"):
        self._config = config

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        message_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        thread_topic: Optional[str] = None,
    ) -> str:
        """Send a UTF-8 email and return the exact outbound Message-ID."""
        if not self._config.smtp_host:
            raise SMTPNotConfiguredError(
                "SMTP_HOST is not configured. Set SMTP_HOST environment variable."
            )

        recipient = validate_mailbox("To", to)
        sender = validate_mailbox("From", self._config.smtp_user, allow_empty=True)
        subject = validate_header_value("Subject", subject)
        outbound_message_id = (
            normalize_message_id(message_id) if message_id else generate_message_id()
        )
        reply_message_id = optional_message_id(in_reply_to, name="In-Reply-To")
        reference_ids = normalize_references(references) if references else None
        outlook_thread_topic = (
            normalize_thread_topic(thread_topic) if thread_topic else None
        )

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        msg["Message-ID"] = outbound_message_id
        if reply_message_id:
            msg["In-Reply-To"] = reply_message_id
        if reference_ids:
            msg["References"] = reference_ids
        if outlook_thread_topic:
            msg["Thread-Topic"] = outlook_thread_topic

        timeout = getattr(self._config, "smtp_timeout_seconds", 30)
        tls_context = ssl.create_default_context()
        if self._config.smtp_tls:
            server = smtplib.SMTP(
                self._config.smtp_host,
                self._config.smtp_port,
                timeout=timeout,
            )
            server.ehlo()
            server.starttls(context=tls_context)
        else:
            server = smtplib.SMTP_SSL(
                self._config.smtp_host,
                self._config.smtp_port,
                timeout=timeout,
                context=tls_context,
            )

        accepted = False
        send_error: BaseException | None = None
        try:
            if self._config.smtp_user and self._config.smtp_pass:
                server.login(self._config.smtp_user, self._config.smtp_pass)
            try:
                server.sendmail(sender, [recipient], msg.as_string())
                accepted = True
            except (
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError,
                smtplib.SMTPHeloError,
            ) as exc:
                raise SMTPSendRejectedError(str(exc)) from exc
            except Exception as exc:
                raise SMTPDeliveryUnknownError(str(exc)) from exc
        except BaseException as exc:
            send_error = exc
            raise
        finally:
            try:
                server.quit()
            except Exception:
                # A failed QUIT after sendmail returned does not revoke the
                # server's prior acceptance. Before acceptance, preserve the
                # original connection/login/send exception instead.
                if not accepted and send_error is None:
                    raise
        return outbound_message_id
