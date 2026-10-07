"""Safe local ingestion and text extraction for POP3 MIME attachments."""
from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Optional

from parsers import parse_file


SUPPORTED_MIME_BY_EXTENSION = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
}
OOXML_MAIN_CONTENT_TYPE_BY_EXTENSION = {
    ".docx": b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ".xlsx": b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ".pptx": b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
GENERIC_DECLARED_MIME_TYPES = {"", "application/octet-stream"}
MAX_ERROR_LENGTH = 1000
MAX_EXTRACTED_TEXT_LENGTH = 100_000


def sanitize_attachment_filename(filename: str, part_index: int = 0) -> str:
    """Return a display/storage basename with traversal and control bytes removed."""
    decoded = unicodedata.normalize("NFC", str(filename or ""))
    basename = re.split(r"[/\\]+", decoded)[-1]
    basename = "".join(
        "_" if ord(char) < 32 or ord(char) == 127 else char
        for char in basename
    )
    basename = basename.replace(":", "_").strip().strip(".")
    if not basename:
        basename = f"attachment-{part_index or 1}"

    suffix = Path(basename).suffix[:20]
    stem_limit = max(1, 180 - len(suffix))
    stem = Path(basename).stem[:stem_limit]
    return f"{stem}{suffix}" if suffix else stem


def _normalized_declared_mime(content_type: str) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower()


def _detect_ooxml_mime(content: bytes) -> tuple[Optional[str], Optional[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 10_000:
                return None, "OpenXML archive has too many entries"
            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > 100 * 1024 * 1024:
                return None, "OpenXML archive expands beyond the safety limit"
            names = {member.filename for member in members}
            content_types = (
                archive.read("[Content_Types].xml").lower()
                if "[Content_Types].xml" in names
                else b""
            )
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None, None

    if "[Content_Types].xml" not in names:
        return None, None
    if (
        "word/document.xml" in names
        and OOXML_MAIN_CONTENT_TYPE_BY_EXTENSION[".docx"] in content_types
    ):
        return SUPPORTED_MIME_BY_EXTENSION[".docx"], None
    if (
        "xl/workbook.xml" in names
        and OOXML_MAIN_CONTENT_TYPE_BY_EXTENSION[".xlsx"] in content_types
    ):
        return SUPPORTED_MIME_BY_EXTENSION[".xlsx"], None
    if (
        "ppt/presentation.xml" in names
        and OOXML_MAIN_CONTENT_TYPE_BY_EXTENSION[".pptx"] in content_types
    ):
        return SUPPORTED_MIME_BY_EXTENSION[".pptx"], None
    return None, None


def detect_actual_mime(content: bytes) -> tuple[Optional[str], Optional[str]]:
    """Detect the four supported formats from file signatures and container layout."""
    if content.startswith(b"%PDF-"):
        return SUPPORTED_MIME_BY_EXTENSION[".pdf"], None
    if content.startswith(b"PK"):
        return _detect_ooxml_mime(content)
    return None, None


def _validate_supported_content(
    filename: str,
    declared_mime: str,
    content: bytes,
) -> tuple[str, str]:
    extension = Path(filename).suffix.lower()
    expected_mime = SUPPORTED_MIME_BY_EXTENSION.get(extension)
    if not expected_mime:
        raise ValueError(
            "unsupported extension; automatic parsing supports only "
            ".docx, .xlsx, .pptx, and .pdf"
        )

    actual_mime, detection_error = detect_actual_mime(content)
    if detection_error:
        raise ValueError(detection_error)
    if actual_mime != expected_mime:
        raise ValueError(f"content signature does not match {extension}")

    normalized_declared = _normalized_declared_mime(declared_mime)
    allowed_declared_mimes = GENERIC_DECLARED_MIME_TYPES | {expected_mime}
    if extension in OOXML_MAIN_CONTENT_TYPE_BY_EXTENSION:
        allowed_declared_mimes.add("application/zip")
    if normalized_declared not in allowed_declared_mimes:
        raise ValueError(
            f"declared MIME {normalized_declared or '(missing)'} does not match {expected_mime}"
        )
    return extension, actual_mime


def _write_exclusive(root: Path, email_id: str, filename: str, content: bytes) -> str:
    root = root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    mail_dir = root / hashlib.sha256(email_id.encode("utf-8")).hexdigest()[:20]
    mail_dir.mkdir(exist_ok=True, mode=0o700)
    resolved_dir = mail_dir.resolve(strict=True)
    if resolved_dir.parent != root:
        raise ValueError("attachment directory escaped configured root")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    for _attempt in range(10):
        candidate = resolved_dir / f"{secrets.token_hex(12)}-{filename}"
        try:
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        return str(candidate)
    raise FileExistsError("could not allocate a collision-free attachment path")


def _error_text(error: Any) -> str:
    text = str(error).replace("\x00", "")
    return text[:MAX_ERROR_LENGTH]


def process_pop3_attachment(
    email_id: str,
    attachment: dict,
    attachment_dir: str,
) -> dict:
    """Validate, store, and extract one attachment without raising parser errors."""
    safe_filename = sanitize_attachment_filename(
        attachment.get("filename", ""),
        int(attachment.get("part_index") or 0),
    )
    content = attachment.get("content")
    base_result = {
        "filename": safe_filename,
        "local_path": None,
        "detected_mime": None,
        "content_sha256": None,
        "extracted_text": None,
        "key_points": None,
        "is_processed": False,
        "processing_status": attachment.get("ingest_status") or "ready",
        "processing_error": attachment.get("ingest_error"),
    }

    if base_result["processing_status"] != "ready":
        return base_result
    if not isinstance(content, bytes):
        base_result.update(
            processing_status="storage_failed",
            processing_error="attachment content was not available",
        )
        return base_result

    try:
        _extension, actual_mime = _validate_supported_content(
            safe_filename,
            attachment.get("content_type", ""),
            content,
        )
    except ValueError as exc:
        status = "unsupported" if "unsupported extension" in str(exc) else "rejected_type"
        base_result.update(
            processing_status=status,
            processing_error=_error_text(exc),
        )
        return base_result

    base_result["detected_mime"] = actual_mime
    base_result["content_sha256"] = hashlib.sha256(content).hexdigest()
    try:
        local_path = _write_exclusive(
            Path(attachment_dir), email_id, safe_filename, content
        )
    except Exception as exc:
        base_result.update(
            processing_status="storage_failed",
            processing_error=_error_text(exc),
        )
        return base_result

    base_result["local_path"] = local_path
    try:
        analysis = parse_file(local_path)
        if analysis is None:
            raise ValueError("parser returned no analysis")
        extracted_text = analysis.extracted_text or ""
        if len(extracted_text) > MAX_EXTRACTED_TEXT_LENGTH:
            extracted_text = extracted_text[:MAX_EXTRACTED_TEXT_LENGTH]
        base_result.update(
            extracted_text=extracted_text,
            is_processed=True,
            processing_status="extracted",
            processing_error=None,
        )
    except Exception as exc:
        base_result.update(
            processing_status="extraction_failed",
            processing_error=_error_text(exc),
        )
    return base_result
