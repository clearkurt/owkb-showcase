"""Small shared helpers: hashing, text IO, ids, git commits, path safety."""

from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config

TZ = timezone(timedelta(hours=8))

TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".py", ".c", ".h", ".inc", ".asm", ".s",
    ".json", ".yaml", ".yml", ".ps1", ".csv", ".ini", ".cfg", ".toml",
    ".jav", ".js", ".ts", ".vue", ".html", ".htm", ".xml",
    ".prj", ".bat", ".sh",
}

SKIP_DIRS = {".git", "__pycache__", ".obsidian", "node_modules", ".idea", ".vscode"}
SKIP_NAMES = {"_index.md", "Thumbs.db", "desktop.ini"}


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def rel_posix(path: Path, base: Path | None = None) -> str:
    base = base or config.ROOT
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTS


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def slug_id(value: str, fallback: str = "doc") -> str:
    """Readable, stable id: keep CJK, collapse separators to '-'."""
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"[\\/:*?\"<>|\s]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or fallback


def safe_under(root: Path, rel: str) -> Path | None:
    """Resolve rel under root; None when it escapes the root."""
    if not rel:
        return root
    rel = rel.replace("\\", "/")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def content_relative(value: str) -> str:
    """Normalise a user supplied content path/pattern to a content-relative posix path.

    Accepts `写法/RCC`, `content/写法/RCC`, `/写法/RCC`, `./content/写法/RCC` and
    backslash forms, so browsing tools all use the same convention.
    """
    path = (value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if path == "content":
        return ""
    if path.startswith("content/"):
        path = path[len("content/"):]
    return path


def git(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_available() -> bool:
    return (config.ROOT / ".git").exists()


def git_commit(message: str) -> str | None:
    """Stage everything and commit when a repo exists. Returns short hash or None."""
    if not git_available():
        return None
    git(["add", "-A"])
    status = git(["status", "--porcelain"])
    if not status.stdout.strip():
        return None
    result = git(["commit", "-m", message])
    if result.returncode != 0:
        return None
    rev = git(["rev-parse", "--short", "HEAD"])
    return rev.stdout.strip()


def ensure_git_identity() -> None:
    if not git_available():
        return
    if not git(["config", "user.name"]).stdout.strip():
        git(["config", "user.name", "OWKB"])
    if not git(["config", "user.email"]).stdout.strip():
        git(["config", "user.email", "owkb@localhost"])
