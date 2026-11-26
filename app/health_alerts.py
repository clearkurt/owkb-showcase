"""Disk / quarantine / notification / service health alerts for OWKB.

Alerts are emailed only to the configured admin recipient.  There is no
team-wide or uploader notification.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, notify, uploads

DISK_WARN_PCT = 85.0
DISK_CRIT_PCT = 92.0
QUARANTINE_WARN_PCT = 70.0
QUARANTINE_CRIT_PCT = 90.0
COOLDOWN_HOURS = 6
STATE_FILE = config.LOGS / "alert_state.json"


def _now() -> datetime:
    return datetime.now().astimezone()


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": [], "last_sent": {}}
    return data if isinstance(data, dict) else {"active": [], "last_sent": {}}


def _write_state(state: dict[str, Any]) -> None:
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def _disk_check() -> dict[str, Any] | None:
    try:
        usage = shutil.disk_usage(config.ROOT)
    except OSError as exc:
        return {"kind": "disk", "severity": "critical", "message": f"无法读取磁盘: {exc}"}
    pct = usage.used / usage.total * 100 if usage.total else 0
    if pct < DISK_WARN_PCT:
        return None
    severity = "critical" if pct >= DISK_CRIT_PCT else "warning"
    return {
        "kind": "disk",
        "severity": severity,
        "message": f"服务器磁盘使用率 {pct:.1f}%（已用 {usage.used / 1024**3:.1f} GB / 共 {usage.total / 1024**3:.1f} GB，剩余 {usage.free / 1024**3:.1f} GB）",
    }


def _quarantine_bytes() -> int:
    total = 0
    try:
        for path in config.UPLOADS.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        pass
    return total


def _quarantine_check() -> dict[str, Any] | None:
    used = _quarantine_bytes()
    limit = uploads.MAX_QUARANTINE_BYTES
    pct = used / limit * 100 if limit else 0
    if pct < QUARANTINE_WARN_PCT:
        return None
    severity = "critical" if pct >= QUARANTINE_CRIT_PCT else "warning"
    return {
        "kind": "quarantine",
        "severity": severity,
        "message": f"_uploads 隔离区 {used / 1024**2:.1f} MB / {limit / 1024**2:.0f} MB（{pct:.1f}%）",
    }


def _queue_check() -> dict[str, Any] | None:
    try:
        events = notify.queue_failures()
    except Exception as exc:
        return {"kind": "notify", "severity": "warning", "message": f"无法读取通知队列: {exc}"}
    if not events:
        return None
    ids = [str(item.get("upload_id", "")) for item in events[:5]]
    return {
        "kind": "notify",
        "severity": "warning",
        "message": f"通知队列有 {len(events)} 条失败邮件（事件: {', '.join(ids)}）",
    }


def _service_check() -> dict[str, Any] | None:
    host, port = config.host_port()
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
    except Exception as exc:
        return {"kind": "service", "severity": "critical", "message": f"OWKB 服务健康检查失败: {exc}"}
    return None


def collect() -> list[dict[str, Any]]:
    checks = [_disk_check(), _quarantine_check(), _queue_check(), _service_check()]
    return [item for item in checks if item]


def check(*, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Check health and email the admin when something is wrong.

    ``dry_run`` never sends email or mutates state.  ``force`` bypasses the
    cooldown, but still only sends when at least one condition is active.
    """
    now = _now()
    alerts = collect()
    state = _read_state()
    previous = set(str(item) for item in state.get("active", []))
    active = {str(item["kind"]) for item in alerts}
    last_sent = state.get("last_sent") if isinstance(state.get("last_sent"), dict) else {}
    due = force or active != previous
    for kind in active:
        stamp = str(last_sent.get(kind, ""))
        try:
            sent_at = datetime.fromisoformat(stamp)
        except ValueError:
            due = True
            continue
        if now - sent_at >= timedelta(hours=COOLDOWN_HOURS):
            due = True

    body_lines = ["OWKB 运维告警", "", f"时间: {now.isoformat(timespec='seconds')}", ""]
    for item in alerts:
        body_lines.append(f"[{str(item.get('severity', 'info')).upper()}] {item.get('message', '')}")
    body = "\n".join(body_lines)

    sent = False
    if alerts and due and not dry_run:
        sent = notify.send_admin_message("运维告警: " + ", ".join(sorted(active)), body)
    elif not alerts and previous and not dry_run:
        # Send one recovery notification after all active alerts clear.
        sent = notify.send_admin_message(
            "运维告警恢复",
            f"OWKB 已恢复正常。\n\n时间: {now.isoformat(timespec='seconds')}\n之前告警: {', '.join(sorted(previous))}",
        )

    if not dry_run:
        if sent:
            for kind in active:
                last_sent[kind] = now.isoformat(timespec="seconds")
        state["active"] = sorted(active)
        state["last_sent"] = last_sent
        state["last_run"] = now.isoformat(timespec="seconds")
        _write_state(state)

    return {
        "ok": not alerts,
        "dry_run": dry_run,
        "forced": force,
        "active": alerts,
        "email_sent": sent,
        "cooldown_hours": COOLDOWN_HOURS,
    }
