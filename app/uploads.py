"""Quarantine uploads and admin review state for OWKB.

Uploaded payloads are written under ``_uploads/<id>/`` and never enter
``content/`` until an admin explicitly approves them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from . import config

_LOCK = threading.RLock()
_ID_RE = re.compile(r"^upload-\d{8}-\d{6}-[0-9a-f]{8}$")
MAX_BYTES = 100 * 1024 * 1024
MAX_UPLOADS_PER_DAY = 30
MAX_BYTES_PER_DAY = 500 * 1024 * 1024
MAX_QUARANTINE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PENDING = 100
_UPLOAD_KINDS = {"project", "manual", "other"}


class UploadError(Exception):
    pass


class UploadTooLarge(UploadError):
    pass


class UploadQuotaExceeded(UploadError):
    pass


class UploadRejected(UploadError):
    pass


class UploadNotFound(UploadError):
    pass


def _upload_root() -> Path:
    root = config.UPLOADS
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_json(path: Path, data: dict[str, Any]) -> None:
    config.ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _safe_filename(filename: str) -> str:
    name = Path((filename or "").strip()).name
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]", "_", name)
    return name[:160] or "payload.bin"


def _bounded(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def _new_id() -> str:
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"upload-{stamp}-{uuid.uuid4().hex[:8]}"


def list_uploads() -> list[dict[str, Any]]:
    root = _upload_root()
    items: list[dict[str, Any]] = []
    for manifest_path in root.glob("*/manifest.json"):
        data = _read_json(manifest_path)
        if data:
            items.append(data)
    items.sort(key=lambda item: str(item.get("created", "")), reverse=True)
    return items


def _check_quota(uploader: str) -> None:
    """Enforce simple filesystem-backed upload quotas."""
    manifests = list_uploads()
    today = datetime.now().astimezone().date().isoformat()
    mine_today = [
        item for item in manifests
        if str(item.get("created", "")).startswith(today) and str(item.get("uploader", "")) == uploader
    ]
    if len(mine_today) >= MAX_UPLOADS_PER_DAY:
        raise UploadQuotaExceeded(f"daily upload count exceeded ({MAX_UPLOADS_PER_DAY})")
    if sum(int(item.get("size", 0) or 0) for item in mine_today) >= MAX_BYTES_PER_DAY:
        raise UploadQuotaExceeded(f"daily upload bytes exceeded ({MAX_BYTES_PER_DAY})")
    total = sum(int(item.get("size", 0) or 0) for item in manifests)
    if total >= MAX_QUARANTINE_BYTES:
        raise UploadQuotaExceeded(f"quarantine disk quota exceeded ({MAX_QUARANTINE_BYTES} bytes)")
    pending = sum(1 for item in manifests if item.get("status") == "pending")
    if pending >= MAX_PENDING:
        raise UploadQuotaExceeded(f"too many pending uploads ({MAX_PENDING})")


def get_upload(upload_id: str) -> dict[str, Any]:
    if not _ID_RE.fullmatch(upload_id or ""):
        raise UploadNotFound(f"invalid upload id: {upload_id}")
    manifest = _read_json(_upload_root() / upload_id / "manifest.json")
    if manifest is None:
        raise UploadNotFound(f"upload not found: {upload_id}")
    return manifest


def payload_path(upload_id: str) -> Path:
    """Return the quarantined payload path for an upload."""
    manifest = get_upload(upload_id)
    name = _safe_filename(str(manifest.get("payload") or manifest.get("filename") or ""))
    path = _upload_root() / upload_id / name
    if not path.is_file():
        raise UploadNotFound(f"upload payload not found: {upload_id}")
    return path


def upload_dir(upload_id: str) -> Path:
    """Return the private quarantine directory for an upload."""
    get_upload(upload_id)
    return _upload_root() / upload_id


def prune(
    *,
    days: int = 30,
    statuses: tuple[str, ...] = ("approved", "rejected"),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove old reviewed upload directories from quarantine.

    Pending uploads are never pruned by default.  Only directories matching the
    upload-id pattern and living directly under ``_uploads`` are eligible.
    """
    if days < 0:
        raise UploadError("days must be >= 0")
    allowed = {"approved", "rejected"}
    selected = tuple(item for item in statuses if item in allowed)
    if not selected:
        raise UploadError("prune statuses must be approved and/or rejected")
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=days)
    root = _upload_root()
    candidates: list[dict[str, Any]] = []
    for manifest in list_uploads():
        if str(manifest.get("status", "")) not in selected:
            continue
        stamp = str(manifest.get("reviewed") or manifest.get("created") or "")
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.astimezone()
        if moment > cutoff:
            continue
        upload_id = str(manifest.get("id", ""))
        if not _ID_RE.fullmatch(upload_id):
            continue
        target = (root / upload_id).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue
        candidates.append({
            "id": upload_id,
            "status": manifest.get("status"),
            "reviewed": manifest.get("reviewed"),
            "size": int(manifest.get("size", 0) or 0),
            "path": target,
        })
    removed: list[str] = []
    freed = 0
    with _LOCK:
        for item in candidates:
            try:
                dir_size = sum(p.stat().st_size for p in item["path"].rglob("*") if p.is_file())
            except Exception:
                continue
            if dry_run:
                removed.append(item["id"])
                freed += dir_size
                continue
            try:
                shutil.rmtree(item["path"])
            except Exception:
                continue
            removed.append(item["id"])
            freed += dir_size
    if removed and not dry_run:
        try:
            from . import audit

            audit.record("upload.pruned", ids=removed, freed_bytes=freed, days=days, statuses=list(selected))
        except Exception:
            pass
    return {
        "dry_run": dry_run,
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "removed": removed,
        "removed_count": len(removed),
        "freed_bytes": freed,
    }


async def save_stream(
    stream: AsyncIterator[bytes],
    *,
    kind: str,
    filename: str,
    name: str,
    mcu: str,
    note: str,
    uploader: str,
    device_id: str,
    device_name: str,
) -> dict[str, Any]:
    kind = kind if kind in _UPLOAD_KINDS else "other"
    payload_name = _safe_filename(filename)
    suffix = Path(payload_name).suffix.lower()
    if kind == "project" and suffix != ".zip":
        raise UploadError("project uploads must be .zip files")
    if kind == "manual" and suffix != ".pdf":
        raise UploadError("manual uploads must be .pdf files")
    if kind == "other" and suffix not in {".zip", ".pdf", ".txt", ".md"}:
        raise UploadError("other uploads must be .zip/.pdf/.txt/.md")
    with _LOCK:
        _check_quota(uploader)
        upload_id = _new_id()
        target_dir = _upload_root() / upload_id
        target_dir.mkdir(parents=True, exist_ok=False)
    payload_path = target_dir / payload_name
    digest = hashlib.sha256()
    size = 0
    try:
        with payload_path.open("wb") as handle:
            async for chunk in stream:
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_BYTES:
                    raise UploadTooLarge(f"upload exceeds {MAX_BYTES} bytes")
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise UploadError("empty upload")

        from . import audit, upload_security

        if kind == "project":
            security = upload_security.inspect_zip(payload_path)
        elif kind == "manual":
            security = upload_security.inspect_pdf(payload_path)
        elif suffix == ".zip":
            security = upload_security.inspect_zip(payload_path)
        elif suffix == ".pdf":
            security = upload_security.inspect_pdf(payload_path)
        else:
            security = {"ok": True, "blocking": False, "findings": [], "files": 1, "skipped": True}
        if security.get("blocking"):
            findings = security.get("findings") or []
            audit.record(
                "upload.blocked",
                upload_id=upload_id,
                uploader=_bounded(uploader, 64),
                device_name=_bounded(device_name, 128),
                kind=kind,
                filename=payload_name,
                findings=[{k: v for k, v in item.items() if k in {"kind", "file", "blocking"}} for item in findings],
            )
            raise UploadRejected("upload rejected by security scan: " + "; ".join(
                f"{item.get('kind')}({item.get('file', '')})" for item in findings[:5]
            ))

        manifest = {
            "id": upload_id,
            "kind": kind,
            "filename": payload_name,
            "payload": payload_name,
            "size": size,
            "sha256": digest.hexdigest(),
            "uploader": _bounded(uploader, 64),
            "device_id": _bounded(device_id, 128),
            "device_name": _bounded(device_name, 128),
            "name": _bounded(name, 200),
            "mcu": _bounded(mcu, 64),
            "note": _bounded(note, 500),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "pending",
            "reviewed": None,
            "security": security,
        }
        _write_json(target_dir / "manifest.json", manifest)
        audit.record(
            "upload.created",
            upload_id=upload_id,
            uploader=manifest["uploader"],
            device_name=manifest["device_name"],
            kind=kind,
            filename=payload_name,
            size=size,
            sha256=manifest["sha256"],
            security_findings=len(security.get("findings") or []),
        )
        try:
            from . import notify

            notify.enqueue_upload(manifest)
        except Exception:
            # Notification is best-effort; the upload itself remains valid.
            pass
        return manifest
    except Exception:
        payload_path.unlink(missing_ok=True)
        manifest_path = target_dir / "manifest.json"
        manifest_path.unlink(missing_ok=True)
        try:
            target_dir.rmdir()
        except OSError:
            pass
        raise


def review(
    upload_id: str,
    status: str,
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"approved", "rejected", "pending"}:
        raise UploadError(f"invalid review status: {status}")
    upload_id = (upload_id or "").strip()
    if not _ID_RE.fullmatch(upload_id):
        raise UploadNotFound(f"invalid upload id: {upload_id}")
    with _LOCK:
        path = _upload_root() / upload_id / "manifest.json"
        manifest = _read_json(path)
        if manifest is None:
            raise UploadNotFound(f"upload not found: {upload_id}")
        manifest["status"] = status
        manifest["reviewed"] = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
        manifest["review_note"] = _bounded(note, 500)
        if extra:
            manifest.update(extra)
        _write_json(path, manifest)
        try:
            from . import audit

            audit.record(
                "upload.review",
                upload_id=upload_id,
                status=status,
                note=note[:500],
                target=(extra or {}).get("import", {}).get("target", "") if extra else "",
            )
        except Exception:
            pass
        return manifest
