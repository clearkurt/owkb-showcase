"""YAML frontmatter parsing/serialisation."""

from __future__ import annotations

from typing import Any

import yaml

DELIM = "---"


def split(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (metadata, body). Tolerates BOM / CRLF and a missing block."""
    text = text.lstrip("\ufeff")
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalised.startswith(DELIM + "\n"):
        return None, text
    end = normalised.find("\n" + DELIM + "\n", len(DELIM) + 1)
    if end == -1:
        return None, text
    raw = normalised[len(DELIM) + 1:end]
    body = normalised[end + len(DELIM) + 2:]
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, text
    if not isinstance(meta, dict):
        return None, text
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


def dumps(meta: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    return f"{DELIM}\n{header}\n{DELIM}\n\n{body.lstrip(chr(10))}"


def normalise_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Coerce common frontmatter fields to predictable shapes."""
    out = dict(meta)
    for key in ("tags", "aliases"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = [v.strip() for v in value.replace("，", ",").split(",") if v.strip()]
        elif value is None:
            out[key] = []
        elif isinstance(value, tuple):
            out[key] = list(value)
    if out.get("updated"):
        out["updated"] = str(out["updated"])
    return out
