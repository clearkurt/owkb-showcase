"""Content scanning, catalog persistence, browsing and file reads."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, extract, frontmatter, util

H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Doc:
    id: str
    path: str  # relative to repository root, posix, starts with "content/"
    title: str
    type: str
    collection: str
    tags: list[str] = field(default_factory=list)
    status: str = "published"
    updated: str = ""
    aliases: list[str] = field(default_factory=list)
    hash: str = ""
    abs_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def rel_to_content(self) -> str:
        prefix = "content/"
        return self.path[len(prefix):] if self.path.startswith(prefix) else self.path

    def to_catalog(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "type": self.type,
            "collection": self.collection,
            "tags": self.tags,
            "status": self.status,
            "updated": self.updated,
            "aliases": self.aliases,
            "hash": self.hash,
            "meta": self.meta,
        }


def load_catalog() -> dict[str, Any]:
    if config.CATALOG_FILE.exists():
        try:
            return json.loads(config.CATALOG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"version": 1, "docs": {}, "aliases": {}, "updated": ""}


def save_catalog(catalog: dict[str, Any]) -> None:
    catalog["updated"] = util.now_iso()
    util.write_text(config.CATALOG_FILE, json.dumps(catalog, ensure_ascii=False, indent=2))


def _iter_text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or not util.is_text_file(path):
            continue
        if any(part in util.SKIP_DIRS for part in path.parts):
            continue
        if path.name in util.SKIP_NAMES:
            continue
        if "snapshots" in path.parts:
            continue
        yield path


def _derive_id(rel_to_content: str, used: set[str]) -> str:
    stem = Path(rel_to_content).with_suffix("").as_posix()
    base = "doc-" + util.slug_id(stem.replace("/", "-"))
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def scan() -> tuple[list[Doc], list[str]]:
    """Scan content/, refresh catalog.json, return (docs, orphan ids)."""
    catalog = load_catalog()
    old_docs: dict[str, Any] = catalog.get("docs", {})
    by_path = {doc.get("path"): doc_id for doc_id, doc in old_docs.items()}
    docs: dict[str, Doc] = {}
    used_ids: set[str] = set()
    seen_paths: set[str] = set()

    for abs_path in _iter_text_files(config.CONTENT):
        rel = abs_path.relative_to(config.ROOT).as_posix()
        seen_paths.add(rel)
        text = util.read_text(abs_path)
        is_md = abs_path.suffix.lower() in {".md", ".markdown"}
        meta: dict[str, Any] = {}
        body = text
        if is_md:
            parsed, body = frontmatter.split(text)
            if parsed:
                meta = frontmatter.normalise_meta(parsed)
        doc_id = str(meta.get("id") or "").strip()
        if not doc_id and by_path.get(rel) in old_docs:
            doc_id = by_path[rel]
        if not doc_id:
            doc_id = _derive_id(rel[len("content/"):], used_ids)
        if doc_id in used_ids:
            doc_id = _derive_id(rel[len("content/"):], used_ids)
        used_ids.add(doc_id)

        title = str(meta.get("title") or "").strip()
        if not title:
            match = H1_RE.search(body)
            title = match.group(1).strip() if match else abs_path.stem
        parts = Path(rel).parts
        collection = str(meta.get("collection") or "").strip()
        if not collection:
            collection = "/".join(parts[1:-1]) or (parts[1] if len(parts) > 1 else "")
        doc_type = str(meta.get("type") or "").strip()
        if not doc_type:
            if extract.is_manual_text(body):
                doc_type = "manual"
            elif abs_path.suffix.lower() in {".md", ".markdown"}:
                doc_type = "doc"
            elif abs_path.suffix.lower() in {".json", ".yaml", ".yml", ".csv", ".ini", ".toml"}:
                doc_type = "data"
            else:
                doc_type = "code"
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        doc = Doc(
            id=doc_id,
            path=rel,
            title=title,
            type=doc_type,
            collection=collection,
            tags=[str(tag) for tag in tags],
            status=str(meta.get("status") or "published"),
            updated=str(meta.get("updated") or util.today()),
            aliases=[str(a) for a in (meta.get("aliases") or [])],
            hash=util.sha256_file(abs_path),
            abs_path=abs_path,
            meta=meta,
        )
        docs[doc_id] = doc

    aliases: dict[str, str] = {}
    for doc_id, doc in docs.items():
        aliases[doc.path] = doc_id
        for alias in doc.aliases:
            aliases[alias] = doc_id

    orphans = [doc_id for doc_id in old_docs if doc_id not in docs]
    catalog = {
        "version": 1,
        "docs": {doc_id: doc.to_catalog() for doc_id, doc in docs.items()},
        "aliases": aliases,
    }
    save_catalog(catalog)
    return list(docs.values()), orphans


def load_docs() -> list[Doc]:
    catalog = load_catalog()
    docs = []
    for doc_id, data in catalog.get("docs", {}).items():
        abs_path = config.ROOT / data["path"]
        docs.append(
            Doc(
                id=doc_id,
                path=data["path"],
                title=data.get("title", ""),
                type=data.get("type", "doc"),
                collection=data.get("collection", ""),
                tags=data.get("tags") or [],
                status=data.get("status", "published"),
                updated=data.get("updated", ""),
                aliases=data.get("aliases") or [],
                hash=data.get("hash", ""),
                abs_path=abs_path if abs_path.exists() else None,
                meta=data.get("meta") or {},
            )
        )
    return docs


def resolve(ref: str) -> Doc | None:
    """Resolve a document by id, path or alias."""
    if not ref:
        return None
    catalog = load_catalog()
    ref = ref.strip().replace("\\", "/")
    if ref in catalog.get("docs", {}):
        return _doc_from_catalog(ref, catalog["docs"][ref])
    if ref in catalog.get("aliases", {}):
        doc_id = catalog["aliases"][ref]
        return _doc_from_catalog(doc_id, catalog["docs"][doc_id])
    candidates = [ref]
    if not ref.startswith("content/"):
        candidates.append("content/" + ref)
    for candidate in candidates:
        doc_id = catalog.get("aliases", {}).get(candidate)
        if doc_id:
            return _doc_from_catalog(doc_id, catalog["docs"][doc_id])
    # fall back: suffix match on path
    for doc_id, data in catalog.get("docs", {}).items():
        if data.get("path", "").endswith("/" + ref) or data.get("path") == ref:
            return _doc_from_catalog(doc_id, data)
    return None


def _doc_from_catalog(doc_id: str, data: dict[str, Any]) -> Doc:
    abs_path = config.ROOT / data["path"]
    return Doc(
        id=doc_id,
        path=data["path"],
        title=data.get("title", ""),
        type=data.get("type", "doc"),
        collection=data.get("collection", ""),
        tags=data.get("tags") or [],
        status=data.get("status", "published"),
        updated=data.get("updated", ""),
        aliases=data.get("aliases") or [],
        hash=data.get("hash", ""),
        abs_path=abs_path if abs_path.exists() else None,
        meta=data.get("meta") or {},
    )


def _image_entries(doc: Doc, text: str) -> list[dict[str, str]]:
    entries = []
    for ref in extract.image_refs(text):
        target = (doc.abs_path.parent / ref["src"]).resolve() if doc.abs_path else None
        if target is None:
            continue
        try:
            rel = target.relative_to(config.CONTENT.resolve()).as_posix()
        except ValueError:
            continue
        entries.append({"alt": ref["alt"], "content_path": rel, "path": f"content/{rel}"})
    return entries


def read(doc: Doc, page: int | None = None, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    """Read a document (or one manual page / line range)."""
    if doc.abs_path is None or not doc.abs_path.exists():
        return {"error": f"file not found: {doc.path}"}
    text = util.read_text(doc.abs_path)
    max_chars = int(config.load_config()["index"].get("max_read_chars", 60000))

    if page is not None:
        pages = extract.chunks_for_text(text, True)
        page_chunks = [chunk for chunk in pages if chunk.kind == "page" and chunk.page_start <= page <= chunk.page_end]
        if not page_chunks:
            available = sorted({chunk.page_label for chunk in pages if chunk.kind == "page"})
            return {"error": f"page {page} not found; available pages: {available[:5]}…{available[-5:]}"}
        chunk = page_chunks[0]
        body = chunk.text
        images = _image_entries(doc, body)
        return {
            "id": doc.id,
            "title": doc.title,
            "path": doc.path,
            "page": chunk.page_label,
            "heading": chunk.heading,
            "text": body[:max_chars],
            "truncated": len(body) > max_chars,
            "images": images,
        }

    meta: dict[str, Any] = {}
    body = text
    if doc.abs_path.suffix.lower() in {".md", ".markdown"}:
        parsed, body = frontmatter.split(text)
        if parsed:
            meta = parsed
    lines = body.splitlines()
    total = len(lines)
    first = max(1, start or 1)
    last = min(total, end or total)
    selected = "\n".join(lines[first - 1:last])
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True
    return {
        "id": doc.id,
        "title": doc.title,
        "path": doc.path,
        "page": None,
        "heading": "",
        "line_start": first,
        "line_end": min(last, total),
        "total_lines": total,
        "text": selected,
        "images": _image_entries(doc, selected),
        "truncated": truncated,
    }


def tree(rel_path: str = "", depth: int = 2) -> dict[str, Any]:
    normalized = util.content_relative(rel_path)
    base = util.safe_under(config.CONTENT, normalized)
    if base is None or not base.exists() or not base.is_dir():
        return {
            "error": f"directory not found: {rel_path}",
            "hint": "path 是相对 content/ 的路径，例如 '写法/RCC'；带不带 content/ 前缀都可以",
        }
    docs = {doc.path: doc for doc in load_docs()}

    def build(directory: Path, level: int) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for entry in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name)):
            if entry.name in util.SKIP_DIRS or entry.name in util.SKIP_NAMES:
                continue
            rel = entry.relative_to(config.CONTENT).as_posix()
            if entry.is_dir():
                if "snapshots" == entry.name:
                    continue
                node = {"type": "dir", "name": entry.name, "path": rel, "children": []}
                if level < depth:
                    node["children"] = build(entry, level + 1)
                children.append(node)
            elif util.is_text_file(entry):
                doc = docs.get("content/" + rel)
                node = {
                    "type": "doc",
                    "name": entry.name,
                    "path": rel,
                    "title": doc.title if doc else entry.stem,
                    "id": doc.id if doc else None,
                    "doc_type": doc.type if doc else None,
                    "size": entry.stat().st_size,
                }
                children.append(node)
        return children

    return {"type": "dir", "name": base.name if normalized else "content", "path": normalized, "children": build(base, 0)}


def ls(rel_path: str = "") -> dict[str, Any]:
    normalized = util.content_relative(rel_path)
    base = util.safe_under(config.CONTENT, normalized)
    if base is None or not base.exists():
        return {
            "error": f"path not found: {rel_path}",
            "hint": "path 是相对 content/ 的路径，例如 '写法/RCC'；带不带 content/ 前缀都可以",
        }
    docs = {doc.path: doc for doc in load_docs()}
    if base.is_file():
        doc = docs.get("content/" + normalized)
        return {"type": "file", "path": normalized, "size": base.stat().st_size, "title": doc.title if doc else None}
    entries = []
    for entry in sorted(base.iterdir(), key=lambda item: (item.is_file(), item.name)):
        if entry.name in util.SKIP_DIRS:
            continue
        rel = entry.relative_to(config.CONTENT).as_posix()
        item: dict[str, Any] = {
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "path": rel,
            "size": 0 if entry.is_dir() else entry.stat().st_size,
        }
        if entry.is_file() and util.is_text_file(entry):
            doc = docs.get("content/" + rel)
            if doc:
                item.update({"title": doc.title, "id": doc.id, "doc_type": doc.type})
        entries.append(item)
    return {"type": "dir", "path": normalized, "entries": entries}


def glob_files(pattern: str, limit: int = 200) -> list[dict[str, Any]]:
    import fnmatch

    pattern = util.content_relative(pattern)
    docs = {doc.path: doc for doc in load_docs()}
    matches = []
    for entry in sorted(config.CONTENT.rglob("*")):
        if not entry.is_file():
            continue
        if any(part in util.SKIP_DIRS for part in entry.parts):
            continue
        rel = entry.relative_to(config.CONTENT).as_posix()
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(entry.name, pattern):
            doc = docs.get("content/" + rel)
            matches.append(
                {
                    "path": rel,
                    "title": doc.title if doc else None,
                    "id": doc.id if doc else None,
                    "size": entry.stat().st_size,
                }
            )
        if len(matches) >= limit:
            break
    return matches


def grep(pattern: str, regex: bool = False, limit: int = 20, context: int = 0) -> dict[str, Any]:
    try:
        compiled = re.compile(pattern, re.IGNORECASE) if regex else None
    except re.error as exc:
        return {"error": f"invalid regex: {exc}"}
    needle = pattern.casefold()
    results = []
    scanned = 0
    for path in _iter_text_files(config.CONTENT):
        scanned += 1
        text = util.read_text(path)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            hit = bool(compiled.search(line)) if compiled else (needle in line.casefold())
            if not hit:
                continue
            start = max(0, index - context)
            end = min(len(lines), index + context + 1)
            results.append(
                {
                    "path": path.relative_to(config.CONTENT).as_posix(),
                    "line": index + 1,
                    "text": line.strip()[:500],
                    "context": lines[start:end],
                }
            )
            if len(results) >= limit:
                return {"pattern": pattern, "matches": results, "truncated": True, "files_scanned": scanned}
    return {"pattern": pattern, "matches": results, "truncated": False, "files_scanned": scanned}


def stat_path(ref: str, repo_relative: bool = False) -> dict[str, Any]:
    doc = resolve(ref)
    if doc is not None:
        return {
            "id": doc.id,
            "path": doc.path,
            "title": doc.title,
            "type": doc.type,
            "collection": doc.collection,
            "tags": doc.tags,
            "status": doc.status,
            "updated": doc.updated,
            "size": doc.abs_path.stat().st_size if doc.abs_path else None,
        }
    base = util.safe_under(config.CONTENT, ref) if not repo_relative else util.safe_under(config.ROOT, ref)
    if base is None or not base.exists():
        return {"error": f"not found: {ref}"}
    return {"path": ref, "size": base.stat().st_size, "is_dir": base.is_dir()}
