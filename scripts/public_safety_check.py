"""Fail CI if the portfolio snapshot contains obvious private data or secrets."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".ini", ".example"
}

# Keep repository-specific private identifiers out of the public snapshot.
FORBIDDEN_LITERALS = (
    "日" + "輝",
    "Ni" + "kki",
    "nikki" + "mfc",
    "nikki" + "system",
    "staging-app." + "nikkisystem.com",
    "/Users/",
    "BEGIN " + "PRIVATE KEY",
)

SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"secret_[A-Za-z0-9_-]{20,}"),
)

EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}))"
)

ALLOWED_EMAIL_DOMAINS = {
    "example.com",
}


def iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path == SELF:
            continue
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        if path.name == "LICENSE" or path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def main() -> int:
    findings: list[str] = []

    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        rel = path.relative_to(ROOT)

        for literal in FORBIDDEN_LITERALS:
            if literal.lower() in text.lower():
                findings.append(f"{rel}: forbidden literal detected: {literal!r}")

        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{rel}: secret-shaped value detected: {pattern.pattern}")

        for address, domain in EMAIL_PATTERN.findall(text):
            if domain.lower() not in ALLOWED_EMAIL_DOMAINS:
                findings.append(f"{rel}: non-placeholder email address detected: {address}")

    if findings:
        print("Public-safety check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Public-safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
