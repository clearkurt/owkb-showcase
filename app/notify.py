"""Email notification queue for OWKB uploads.

The upload endpoint only appends an event here. A cron job runs
``kb uploads notify``, which sends the messages asynchronously and never
blocks the upload itself.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import threading
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml

from . import config

_LOCK = threading.RLock()
_MAX_ATTEMPTS = 3


def _queue_path() -> Path:
    config.UPLOADS.mkdir(parents=True, exist_ok=True)
    return config.UPLOADS / "notifications.json"


def _load_queue() -> list[dict[str, Any]]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_queue(events: list[dict[str, Any]]) -> None:
    path = _queue_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def enqueue_upload(manifest: dict[str, Any]) -> None:
    """Append a pending upload notification; best-effort and idempotent."""
    upload_id = str(manifest.get("id", ""))
    if not upload_id:
        return
    with _LOCK:
        events = _load_queue()
        if any(event.get("upload_id") == upload_id and event.get("event") == "upload.created" for event in events):
            return
        events.append(
            {
                "id": uuid.uuid4().hex,
                "event": "upload.created",
                "upload_id": upload_id,
                "created": manifest.get("created", ""),
                "status": "pending",
                "attempts": 0,
                "last_error": "",
            }
        )
        _save_queue(events)


def queue_failures() -> list[dict[str, Any]]:
    """Return notification events that exhausted retries or failed."""
    with _LOCK:
        events = _load_queue()
    return [
        event for event in events
        if event.get("status") == "failed" or int(event.get("attempts", 0) or 0) >= _MAX_ATTEMPTS
    ]


def _email_config() -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(config.NOTIFY_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    email_cfg = raw.get("email") if isinstance(raw, dict) else None
    if not isinstance(email_cfg, dict) or not email_cfg.get("enabled", False):
        return None
    return email_cfg


def _send_email(email_cfg: dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    prefix = str(email_cfg.get("subject_prefix", "[OWKB]"))
    msg["Subject"] = f"{prefix} {subject}"
    msg["From"] = str(email_cfg.get("from") or email_cfg.get("username"))
    recipients = email_cfg.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [str(item).strip() for item in recipients if str(item).strip()]
    if not recipients:
        raise RuntimeError("notify email.to is empty")
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    host = str(email_cfg.get("smtp_host", "smtp.qq.com"))
    port = int(email_cfg.get("smtp_port", 465))
    username = str(email_cfg.get("username", ""))
    password = str(email_cfg.get("password", ""))
    context = ssl.create_default_context()
    if bool(email_cfg.get("smtp_ssl", True)):
        with smtplib.SMTP_SSL(host, port, timeout=25, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(msg)


def send_admin_message(subject: str, body: str) -> bool:
    """Send an operational message to the configured admin only.

    Returns False when email is disabled, unconfigured, or SMTP fails.
    """
    email_cfg = _email_config()
    if email_cfg is None:
        return False
    try:
        _send_email(email_cfg, subject, body)
    except Exception:
        return False
    return True


def _upload_body(manifest: dict[str, Any]) -> tuple[str, str]:
    kind = str(manifest.get("kind", "other"))
    labels = {"project": "项目源码", "manual": "芯片手册 PDF 原件", "other": "其他资料"}
    label = labels.get(kind, "其他资料")
    upload_id = str(manifest.get("id", ""))
    size = int(manifest.get("size", 0) or 0)
    subject = f"新上传待审核 {upload_id}"
    lines = [
        "OWKB 收到一个新的待审核上传。",
        "",
        f"ID: {upload_id}",
        f"类型: {label}",
        f"上传人: {manifest.get('uploader') or '-'}",
        f"设备: {manifest.get('device_name') or '-'}",
        f"项目/名称: {manifest.get('name') or '-'}",
        f"MCU: {manifest.get('mcu') or '-'}",
        f"文件名: {manifest.get('filename') or '-'}",
        f"大小: {size / 1024:.1f} KB",
        f"时间: {manifest.get('created') or '-'}",
        "",
        "审核命令:",
        "ssh <your-server>",
        "cd <owkb-root>",
        f"venv/bin/python -m app.cli uploads show {upload_id}",
        f"venv/bin/python -m app.cli uploads approve {upload_id}",
        f"venv/bin/python -m app.cli uploads reject {upload_id}",
        "",
        "注意: 上传文件在 _uploads 隔离区，不会自动进入 content/。",
    ]
    note = manifest.get("note")
    if note:
        lines.extend(["", f"上传备注: {note}"])
    security = manifest.get("security") or {}
    findings = security.get("findings") if isinstance(security, dict) else None
    if findings:
        lines.extend(["", "安全扫描提示:"])
        for item in findings[:10]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('kind', 'finding')}({item.get('file', '')})")
    return subject, "\n".join(lines)


def send_pending(limit: int = 20) -> dict[str, int]:
    """Send pending emails; returns counts and never raises on SMTP failure."""
    email_cfg = _email_config()
    if email_cfg is None:
        return {"pending": 0, "sent": 0, "failed": 0, "skipped": 1}
    stats = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    with _LOCK:
        events = _load_queue()
        for event in events:
            if stats["sent"] + stats["failed"] >= limit:
                break
            if event.get("status") == "sent":
                continue
            if int(event.get("attempts", 0)) >= _MAX_ATTEMPTS:
                continue
            if event.get("event") != "upload.created":
                continue
            stats["pending"] += 1
            try:
                from . import uploads

                manifest = uploads.get_upload(str(event.get("upload_id", "")))
                subject, body = _upload_body(manifest)
                _send_email(email_cfg, subject, body)
            except Exception as exc:
                event["attempts"] = int(event.get("attempts", 0)) + 1
                event["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                event["status"] = "failed" if event["attempts"] >= _MAX_ATTEMPTS else "pending"
                stats["failed"] += 1
            else:
                event["status"] = "sent"
                event["sent_at"] = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
                stats["sent"] += 1
        _save_queue(events)
    return stats
