"""Token lifecycle and self-enrollment support for OWKB."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Any

from . import config, util

_LOCK = threading.RLock()
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,15}$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TOKEN_PREFIX = "owkb-ro-"
ENROLL_PREFIX = "owkb-enroll-"


class TokenError(Exception):
    pass


class TokenExists(TokenError):
    pass


def _payload() -> dict[str, Any]:
    if not config.TOKENS_FILE.exists():
        return {"tokens": []}
    try:
        data = json.loads(config.TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tokens": []}
    if not isinstance(data, dict):
        return {"tokens": []}
    data.setdefault("tokens", [])
    return data


def _save(payload: dict[str, Any]) -> None:
    config.ensure_dirs()
    util.write_text(config.TOKENS_FILE, json.dumps(payload, ensure_ascii=False, indent=2))


def normalize_name(name: str) -> str:
    value = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(value):
        raise TokenError("缩写只能是 2-16 位小写字母/数字/下划线/短横线，且以字母开头")
    return value


def normalize_device_id(device_id: str) -> str:
    value = (device_id or "").strip()
    if not _DEVICE_RE.fullmatch(value):
        raise TokenError("device_id 必须是 16-128 位字母、数字、下划线或短横线")
    return value


def list_tokens() -> list[dict[str, Any]]:
    with _LOCK:
        items = []
        for item in _payload().get("tokens", []):
            items.append(
                {
                    "name": item.get("name"),
                    "owner": item.get("owner") or "",
                    "note": item.get("note") or "",
                    "created": item.get("created"),
                    "readonly": bool(item.get("readonly", True)),
                    "device_bound": bool(item.get("device_id")),
                    "device_id": item.get("device_id") or "",
                    "device_name": item.get("device_name") or "",
                }
            )
        return items


def find_token(name: str) -> dict[str, Any] | None:
    with _LOCK:
        for item in _payload().get("tokens", []):
            if item.get("name") == name:
                return item
    return None


def create_token(
    name: str,
    *,
    owner: str = "",
    note: str = "",
    rotate_if_owner: bool = False,
    device_id: str = "",
    device_name: str = "",
) -> tuple[dict[str, Any], str]:
    """Create (or rotate an owner-matched) token; return (entry, plaintext token)."""
    name = normalize_name(name)
    owner = (owner or "").strip()[:128]
    note = (note or "").strip()[:200]
    device_id = normalize_device_id(device_id) if device_id else ""
    device_name = (device_name or "").strip()[:128]
    with _LOCK:
        payload = _payload()
        tokens = payload.setdefault("tokens", [])
        existing = next((item for item in tokens if item.get("name") == name), None)
        if existing is not None:
            same_owner = owner and (existing.get("owner") or "") == owner
            if not (rotate_if_owner and same_owner):
                raise TokenExists(f"token name '{name}' already exists")
            tokens.remove(existing)
        token = TOKEN_PREFIX + secrets.token_urlsafe(24)
        entry = {
            "name": name,
            "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "groups": ["all"],
            "readonly": True,
            "created": util.now_iso(),
            "owner": owner,
            "note": note,
            "device_id": device_id,
            "device_name": device_name,
        }
        tokens.append(entry)
        _save(payload)
    return entry, token


def rotate_token(name: str) -> tuple[dict[str, Any], str] | None:
    name = normalize_name(name)
    with _LOCK:
        payload = _payload()
        tokens = payload.setdefault("tokens", [])
        existing = next((item for item in tokens if item.get("name") == name), None)
        if existing is None:
            return None
        tokens.remove(existing)
        token = TOKEN_PREFIX + secrets.token_urlsafe(24)
        existing["sha256"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
        existing["created"] = util.now_iso()
        tokens.append(existing)
        _save(payload)
    return existing, token


def delete_token(name: str) -> bool:
    name = normalize_name(name)
    with _LOCK:
        payload = _payload()
        tokens = payload.setdefault("tokens", [])
        kept = [item for item in tokens if item.get("name") != name]
        if len(kept) == len(tokens):
            return False
        payload["tokens"] = kept
        _save(payload)
    return True


def bind_token_device(name: str, device_id: str, device_name: str = "") -> dict[str, Any] | None:
    """Bind a token to a device id. Same device id is idempotent."""
    name = normalize_name(name)
    device_id = normalize_device_id(device_id)
    device_name = (device_name or "").strip()[:128]
    with _LOCK:
        payload = _payload()
        for item in payload.get("tokens", []):
            if item.get("name") != name:
                continue
            current = str(item.get("device_id") or "")
            if current and current != device_id:
                raise TokenError("token is already bound to another device")
            item["device_id"] = device_id
            if device_name:
                item["device_name"] = device_name
            _save(payload)
            return dict(item)
    return None


def unbind_token_device(name: str) -> dict[str, Any] | None:
    name = normalize_name(name)
    with _LOCK:
        payload = _payload()
        for item in payload.get("tokens", []):
            if item.get("name") != name:
                continue
            item["device_id"] = ""
            item["device_name"] = ""
            _save(payload)
            return dict(item)
    return None


def lookup_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _LOCK:
        for item in _payload().get("tokens", []):
            expected = str(item.get("sha256", "")).lower()
            if expected and hmac.compare_digest(digest, expected):
                return dict(item)
    return None


def verify_token(token: str) -> str | None:
    entry = lookup_token(token)
    return str(entry.get("name", "unknown")) if entry is not None else None


def token_hashes() -> list[tuple[str, str]]:
    with _LOCK:
        return [
            (str(item.get("name", "unknown")), str(item.get("sha256", "")).lower())
            for item in _payload().get("tokens", [])
            if item.get("sha256")
        ]


def _enroll_payload() -> dict[str, Any]:
    if not config.ENROLL_FILE.exists():
        return {}
    try:
        data = json.loads(config.ENROLL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_enroll_code() -> str:
    """Return the existing enrollment code, creating one if needed."""
    with _LOCK:
        payload = _enroll_payload()
        if config.ENROLL_PLAIN_FILE.exists() and payload.get("sha256"):
            code = config.ENROLL_PLAIN_FILE.read_text(encoding="utf-8").strip()
            if code and hmac.compare_digest(
                hashlib.sha256(code.encode("utf-8")).hexdigest(), payload["sha256"]
            ):
                return code
        code = ENROLL_PREFIX + secrets.token_urlsafe(24)
        config.ensure_dirs()
        util.write_text(
            config.ENROLL_FILE,
            json.dumps(
                {
                    "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    "created": util.now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        config.ENROLL_PLAIN_FILE.write_text(code + "\n", encoding="utf-8")
        return code


def verify_enroll_code(code: str) -> bool:
    if not code:
        return False
    payload = _enroll_payload()
    expected = str(payload.get("sha256", "")).lower()
    if not expected:
        return False
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected)


def rotate_enroll_code() -> str:
    with _LOCK:
        code = ENROLL_PREFIX + secrets.token_urlsafe(24)
        config.ensure_dirs()
        util.write_text(
            config.ENROLL_FILE,
            json.dumps(
                {
                    "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    "created": util.now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        config.ENROLL_PLAIN_FILE.write_text(code + "\n", encoding="utf-8")
        return code
