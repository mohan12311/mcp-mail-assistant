"""Validation and generation helpers for outbound RFC email headers."""
from __future__ import annotations

from email.headerregistry import Address
from email.utils import make_msgid
from typing import Optional


class UnsafeEmailHeaderError(ValueError):
    """Raised when untrusted data cannot safely be used as an email header."""


def validate_header_value(name: str, value: str, *, allow_empty: bool = False) -> str:
    """Return *value* after rejecting characters that can create new headers."""
    if not isinstance(value, str):
        raise UnsafeEmailHeaderError(f"{name} must be a string")
    if not allow_empty and not value:
        raise UnsafeEmailHeaderError(f"{name} must not be empty")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise UnsafeEmailHeaderError(f"{name} contains prohibited control characters")
    return value


def validate_mailbox(name: str, value: str, *, allow_empty: bool = False) -> str:
    """Validate a single addr-spec suitable for both a header and SMTP envelope."""
    value = validate_header_value(name, value, allow_empty=allow_empty).strip()
    if not value and allow_empty:
        return ""
    try:
        address = Address(addr_spec=value)
    except (IndexError, TypeError, ValueError) as exc:
        raise UnsafeEmailHeaderError(f"{name} is not a single valid mailbox") from exc
    if address.addr_spec != value:
        raise UnsafeEmailHeaderError(f"{name} is not a canonical mailbox")
    return value


def normalize_message_id(value: str, *, name: str = "Message-ID") -> str:
    """Validate and normalize one conservative RFC 5322 Message-ID token."""
    candidate = validate_header_value(name, value).strip()
    if len(candidate) > 998 or not (candidate.startswith("<") and candidate.endswith(">")):
        raise UnsafeEmailHeaderError(f"{name} is not a valid Message-ID")

    inner = candidate[1:-1]
    local, separator, domain = inner.partition("@")
    if (
        not separator
        or not local
        or not domain
        or "@" in domain
        or any(char in "<>" or ord(char) < 33 or ord(char) > 126 for char in inner)
    ):
        raise UnsafeEmailHeaderError(f"{name} is not a valid Message-ID")
    return candidate


def normalize_references(value: str) -> str:
    """Validate a whitespace-separated References header value."""
    value = validate_header_value("References", value).strip()
    tokens = value.split()
    if not tokens:
        raise UnsafeEmailHeaderError("References must not be empty")
    return " ".join(
        normalize_message_id(token, name="References") for token in tokens
    )


def build_reply_references(parent_references: Optional[str], parent_message_id: str) -> str:
    """Build RFC 5322 reply References as parent chain followed by the parent ID."""
    parent_id = normalize_message_id(parent_message_id, name="parent Message-ID")
    chain = (
        normalize_references(parent_references).split()
        if parent_references
        else []
    )
    result: list[str] = []
    seen: set[str] = set()
    for message_id in [*chain, parent_id]:
        if message_id in seen:
            continue
        seen.add(message_id)
        result.append(message_id)
    return " ".join(result)


def normalize_thread_topic(value: str) -> str:
    """Validate an Outlook Thread-Topic while preserving its Unicode value."""
    topic = validate_header_value("Thread-Topic", value)
    if not topic.strip() or len(topic) > 998:
        raise UnsafeEmailHeaderError("Thread-Topic is empty or too long")
    return topic


def optional_message_id(value: Optional[str], *, name: str) -> Optional[str]:
    """Validate a present Message-ID, preserving absence for non-threaded mail."""
    if value is None or value == "":
        return None
    return normalize_message_id(value, name=name)


def generate_message_id(*, task_id: Optional[int] = None) -> str:
    """Generate an opaque outbound Message-ID without exposing the local hostname."""
    idstring = f"task-{int(task_id)}" if task_id is not None else None
    return normalize_message_id(
        make_msgid(idstring=idstring, domain="mcp-mail-assistant.invalid")
    )
