"""Background content watcher for OWKB.

Started by the server (app/server.py). Every `index.auto_reindex_interval` seconds
(default 60) it computes a cheap signature of all indexed content files; when the
signature changes it rebuilds the SQLite index and commits the content changes.

The rebuild is serialized with search()/stats() through index._LOCK, so requests see
either the old or the new index, never a half-built one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from . import config, index, util

SIGNATURE_FILE = config.DATA / "index_signature.json"
LOG_FILE = config.LOGS / "auto_index.log"
DEFAULT_INTERVAL = 60

_START_LOCK = threading.Lock()
_CHECK_LOCK = threading.Lock()
_STARTED = False


def _log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _content_signature() -> str:
    parts: list[str] = []
    for path in sorted(config.CONTENT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in util.SKIP_DIRS for part in path.parts):
            continue
        if path.name in util.SKIP_NAMES:
            continue
        if not util.is_text_file(path):
            continue
        stat = path.stat()
        rel = path.relative_to(config.CONTENT).as_posix()
        parts.append(f"{rel}\t{stat.st_mtime_ns}\t{stat.st_size}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(SIGNATURE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    SIGNATURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNATURE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _index_meta() -> dict[str, Any]:
    if not config.INDEX_FILE.exists():
        return {}
    conn = sqlite3.connect(config.INDEX_FILE)
    try:
        return {key: value for key, value in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _commit_content(message: str) -> str | None:
    """Commit only content + generated catalog, never unrelated code edits."""
    if not util.git_available():
        return None
    util.git(["add", "-A", "content", "data/catalog.json"])
    status = util.git(["status", "--porcelain"])
    if not status.stdout.strip():
        return None
    result = util.git(["commit", "-m", message])
    if result.returncode != 0:
        return None
    return util.git(["rev-parse", "--short", "HEAD"]).stdout.strip()


def check_once(force: bool = False) -> dict[str, Any]:
    """Check content signature; rebuild + commit if it changed."""
    with _CHECK_LOCK:
        signature = _content_signature()
        state = _load_state()
        if not force and state.get("signature") == signature and config.INDEX_FILE.exists():
            return {"changed": False, "signature": signature, "indexed_at": state.get("indexed_at")}

        _log("content changed (or no stored signature); rebuilding index")
        try:
            meta = index.build()
            commit = _commit_content("kb-ops: auto reindex")
            state = {
                "signature": signature,
                "indexed_at": util.now_iso(),
                "checked_at": util.now_iso(),
                "docs": meta.get("docs"),
                "chunks": meta.get("chunks"),
                "commit": commit,
            }
            _save_state(state)
            _log(f"reindex ok docs={state['docs']} chunks={state['chunks']} commit={commit or '-'}")
            return {"changed": True, "signature": signature, "index": meta, "commit": commit}
        except Exception as exc:  # keep the watcher alive; retry on next interval
            _log(f"reindex failed: {type(exc).__name__}: {exc}")
            return {"changed": True, "signature": signature, "error": f"{type(exc).__name__}: {exc}"}


def _loop(interval: int) -> None:
    _log(f"auto-index watcher started (interval={interval}s)")
    time.sleep(5)
    while True:
        try:
            check_once()
        except Exception as exc:  # pragma: no cover - last-resort guard
            _log(f"watcher loop error: {type(exc).__name__}: {exc}")
        time.sleep(interval)


def start() -> None:
    """Start the watcher once per process (no-op on repeated calls)."""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
    interval = int(config.load_config().get("index", {}).get("auto_reindex_interval", DEFAULT_INTERVAL))
    thread = threading.Thread(target=_loop, args=(max(interval, 10),), name="owkb-autoindex", daemon=True)
    thread.start()
