"""Configuration and path constants for the OWKB knowledge base."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

CONTENT = ROOT / "content"
LIBRARY = ROOT / "library"
DATA = ROOT / "data"
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"
DIST_DIR = ROOT / "dist"
SCRIPTS_DIR = ROOT / "scripts"
INBOX = ROOT / "inbox"
TRASH = ROOT / "_trash"
LOGS = ROOT / "logs"
UPLOADS = ROOT / "_uploads"

TOKENS_FILE = CONFIG_DIR / "tokens.json"
TOKEN_PLAIN_FILE = CONFIG_DIR / "token-readonly.txt"
ENROLL_FILE = CONFIG_DIR / "enroll.json"
ENROLL_PLAIN_FILE = CONFIG_DIR / "enroll-code.txt"
NOTIFY_FILE = CONFIG_DIR / "notify.yaml"
KB_YAML = CONFIG_DIR / "kb.yaml"
CATALOG_FILE = DATA / "catalog.json"
IMPORTS_FILE = DATA / "imports.json"
INDEX_FILE = DATA / "index.sqlite"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8787, "public_base_url": ""},
    "paths": {
        "content": "content",
        "library": "library",
    },
    "index": {
        "snippet_width": 180,
        "max_read_chars": 60000,
    },
    "collections": ["00-总览", "芯片手册", "写法", "项目案例"],
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    """Load config/kb.yaml merged over defaults."""
    if KB_YAML.exists():
        raw = yaml.safe_load(KB_YAML.read_text(encoding="utf-8")) or {}
        return _deep_merge(DEFAULTS, raw)
    return dict(DEFAULTS)


def ensure_dirs() -> None:
    for path in (CONTENT, LIBRARY, DATA, CONFIG_DIR, INBOX, TRASH, LOGS):
        path.mkdir(parents=True, exist_ok=True)


def host_port() -> tuple[str, int]:
    cfg = load_config()
    return str(cfg["server"]["host"]), int(cfg["server"]["port"])
