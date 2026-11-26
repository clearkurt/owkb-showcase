"""Append-only JSONL audit log for upload and review operations.

The audit log deliberately records the token *name* and device name, never the
bearer token itself.  Logging failures are best-effort and must never break an
upload or an admin review.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from . import config

_LOCK = threading.RLock()


def record(event: str, **fields: Any) -> None:
    """Append one audit record; never raises."""
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            with (config.LOGS / "audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        # Auditing is best-effort; never make an upload fail because logging did.
        pass


def rotate(*, max_bytes: int = 10 * 1024 * 1024, keep: int = 5) -> dict[str, Any]:
    """Rotate audit.jsonl when it exceeds max_bytes.

    Archives are named ``audit-YYYYmmdd-HHMMSS.jsonl``.  ``keep`` is the number
    of archives retained; the oldest archives are deleted after rotation.
    Returns a small JSON-friendly summary; never raises.
    """
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        current = config.LOGS / "audit.jsonl"
        if not current.exists() or current.stat().st_size <= max_bytes:
            return {"rotated": False, "size": current.stat().st_size if current.exists() else 0, "removed": []}
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        archive = config.LOGS / f"audit-{stamp}.jsonl"
        counter = 2
        while archive.exists():
            archive = config.LOGS / f"audit-{stamp}-{counter}.jsonl"
            counter += 1
        with _LOCK:
            current.replace(archive)
            archives = sorted(config.LOGS.glob("audit-*.jsonl"), key=lambda p: p.name)
            removed: list[str] = []
            while len(archives) > max(1, int(keep)):
                oldest = archives.pop(0)
                oldest.unlink(missing_ok=True)
                removed.append(oldest.name)
        record("audit.rotated", archive=archive.name, removed=removed)
        return {
            "rotated": True,
            "archive": archive.name,
            "size": archive.stat().st_size,
            "kept": len(archives),
            "removed": removed,
        }
    except Exception as exc:
        return {"rotated": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
