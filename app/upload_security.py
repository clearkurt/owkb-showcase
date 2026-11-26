"""Static security checks for quarantined uploads.

This module only *inspects* a payload.  It does not import anything into
``content/`` and it does not parse PDF text (PDF parsing belongs to the
external RAG pipeline).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

MAX_EXPANDED_BYTES = 500 * 1024 * 1024
MAX_FILES = 5000
MAX_COMPRESSION_RATIO = 200
MAX_SCAN_FILE_BYTES = 512 * 1024

_SENSITIVE_NAMES = {
    ".env",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "wp-config.php",
}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_TEXT_SUFFIXES = {
    ".asm", ".c", ".h", ".inc", ".md", ".txt", ".json", ".yaml", ".yml",
    ".ini", ".toml", ".py", ".js", ".ts", ".sh", ".ps1", ".bat", ".cmd",
    ".cs", ".cpp", ".hpp", ".xml", ".html", ".css", ".csv", ".env",
}
_SECRET_PATTERNS = [
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("google_api_key", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    ("generic_secret", re.compile(
        rb"(?i)(?:api[_-]?key|secret|access[_-]?token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
    )),
]
_BLOCKING_KINDS = {"private_key", "aws_access_key", "github_token", "slack_token", "google_api_key"}
_BLOCKING_FILE = "sensitive_file"
_MAX_FINDINGS = 20


def _sensitive_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    return base in _SENSITIVE_NAMES or any(base.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _scan_content(name: str, data: bytes) -> list[dict[str, str]]:
    suffix = Path(name).suffix.lower()
    if suffix not in _TEXT_SUFFIXES or len(data) > MAX_SCAN_FILE_BYTES:
        return []
    findings: list[dict[str, str]] = []
    for kind, pattern in _SECRET_PATTERNS:
        match = pattern.search(data)
        if match:
            findings.append({"kind": kind, "file": name, "blocking": str(kind in _BLOCKING_KINDS).lower()})
    # Match lines are not recorded: the audit log must not contain the secret.
    return findings


def inspect_zip(payload: Path) -> dict[str, Any]:
    """Validate and scan a project zip; returns findings and stats."""
    if not zipfile.is_zipfile(payload):
        return {"ok": False, "blocking": True, "findings": [{"kind": "not_zip", "file": "", "blocking": "true"}]}

    findings: list[dict[str, str]] = []
    total = 0
    compressed = 0
    count = 0
    try:
        with zipfile.ZipFile(payload) as archive:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                count += 1
                if count > MAX_FILES:
                    findings.append({"kind": "too_many_files", "file": "", "blocking": "true"})
                    break
                total += int(info.file_size)
                compressed += int(info.compress_size)
                if total > MAX_EXPANDED_BYTES:
                    findings.append({"kind": "expanded_too_large", "file": "", "blocking": "true"})
                    break
                if _sensitive_name(name):
                    findings.append({"kind": _BLOCKING_FILE, "file": name[:300], "blocking": "true"})
                    continue
                if Path(name).suffix.lower() in _TEXT_SUFFIXES and info.file_size <= MAX_SCAN_FILE_BYTES:
                    try:
                        with archive.open(info) as handle:
                            data = handle.read(MAX_SCAN_FILE_BYTES + 1)
                        if len(data) <= MAX_SCAN_FILE_BYTES:
                            findings.extend(_scan_content(name, data))
                    except Exception:
                        findings.append({"kind": "unreadable_entry", "file": name[:300], "blocking": "true"})
                if len(findings) >= _MAX_FINDINGS:
                    break
    except Exception as exc:
        findings.append({"kind": "zip_error", "file": "", "blocking": "true", "message": str(exc)[:200]})

    ratio = total / max(compressed, 1)
    if ratio > MAX_COMPRESSION_RATIO:
        findings.append({"kind": "compression_ratio", "file": "", "blocking": "true"})
    blocking = any(item.get("blocking") == "true" for item in findings)
    return {
        "ok": not blocking,
        "blocking": blocking,
        "findings": findings[:_MAX_FINDINGS],
        "files": count,
        "expanded_bytes": total,
        "compression_ratio": round(ratio, 2),
    }


def inspect_pdf(payload: Path) -> dict[str, Any]:
    """Validate only the PDF signature; do not extract or parse text."""
    try:
        header = payload.read_bytes()[:5]
    except OSError:
        return {"ok": False, "blocking": True, "findings": [{"kind": "unreadable_pdf", "file": "", "blocking": "true"}]}
    if header != b"%PDF-":
        return {"ok": False, "blocking": True, "findings": [{"kind": "not_pdf", "file": "", "blocking": "true"}]}
    return {"ok": True, "blocking": False, "findings": [], "files": 1, "expanded_bytes": payload.stat().st_size, "compression_ratio": 0}
