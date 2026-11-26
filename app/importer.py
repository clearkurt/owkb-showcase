"""Import / create / move / delete / TOC helpers for the knowledge base."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, frontmatter, store, util

H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _load_imports() -> dict[str, Any]:
    if config.IMPORTS_FILE.exists():
        try:
            return json.loads(config.IMPORTS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"version": 1, "imports": {}}


def _save_imports(imports: dict[str, Any]) -> None:
    util.write_text(config.IMPORTS_FILE, json.dumps(imports, ensure_ascii=False, indent=2))


def _title_from_text(text: str, fallback: str) -> str:
    match = H1_RE.search(text)
    if match:
        return match.group(1).strip()
    return fallback


def _group_tags(rel: str) -> list[str]:
    lower = rel.casefold()
    tags: list[str] = []
    stem = Path(rel).stem
    if rel.startswith("cases/"):
        tags.append("案例")
    elif rel.startswith("evidence/"):
        tags.append("证据")
    if re.match(r"^K\d", stem):
        tags.append("RCC内核")
    elif re.match(r"^B\d", stem):
        tags.append("RCC绑定")
    elif stem.startswith("P-"):
        tags.append("RCC移植")
    elif stem.startswith("V-"):
        tags.append("RCC验证")
    elif stem.startswith("00"):
        tags.append("RCC总纲")
    return tags


def _derive_id(prefix: str, rel: str, used: set[str]) -> str:
    stem = Path(rel).with_suffix("").as_posix().replace("/", "-")
    base = f"{prefix}-{util.slug_id(stem)}" if prefix else util.slug_id(stem)
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def prepare_markdown(
    source_text: str,
    *,
    doc_id: str,
    title: str,
    collection: str,
    tags: list[str],
    source: str,
    doc_type: str = "doc",
    updated: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Ensure a markdown text has frontmatter, preserving an existing block."""
    collection = collection.replace("\\", "/").strip("/")
    meta, body = frontmatter.split(source_text)
    if meta is None:
        meta = {}
        body = source_text
    meta = dict(meta)
    meta.setdefault("id", doc_id)
    meta.setdefault("title", title)
    meta.setdefault("type", doc_type)
    meta.setdefault("collection", collection)
    meta.setdefault("tags", tags)
    meta.setdefault("status", "published")
    meta.setdefault("updated", updated or util.today())
    meta.setdefault("source", source)
    for key, value in (extra or {}).items():
        meta.setdefault(key, value)
    return frontmatter.dumps(meta, body)


def _copy_asset(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.stat().st_size


def import_manual(
    source_md: Path,
    snapshots_dir: Path | None,
    dest_dir: Path,
    *,
    doc_id: str,
    title: str,
    collection: str,
    tags: list[str],
    page_count: int | None,
    pdf_source: Path | None,
    pdf_library_rel: str,
    source_note: str,
) -> dict[str, Any]:
    """Import one converted manual: full.md + snapshots + original PDF."""
    if not source_md.exists():
        raise FileNotFoundError(source_md)
    rel_dest = dest_dir.relative_to(config.CONTENT).as_posix()
    target_md = dest_dir / "full.md"
    text = util.read_text(source_md)
    # Snapshots are copied next to full.md, while the converted source lived in md/.
    text = text.replace("](../snapshots/", "](snapshots/").replace('src="../snapshots/', 'src="snapshots/')
    extra: dict[str, Any] = {"source": source_note}
    if pdf_source is not None:
        extra["source_pdf"] = pdf_library_rel
    if page_count is not None:
        extra["page_count"] = page_count
    payload = prepare_markdown(
        text,
        doc_id=doc_id,
        title=title,
        collection=collection,
        tags=tags,
        source=source_note,
        doc_type="manual",
        extra=extra,
    )
    util.write_text(target_md, payload)
    copied_images = 0
    if snapshots_dir and snapshots_dir.exists():
        target_snapshots = dest_dir / "snapshots"
        if target_snapshots.exists():
            shutil.rmtree(target_snapshots)
        shutil.copytree(snapshots_dir, target_snapshots)
        copied_images = len(list(target_snapshots.iterdir()))
    if pdf_source is not None and pdf_source.exists():
        library_target = config.ROOT / pdf_library_rel
        library_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_source, library_target)
    imports = _load_imports()
    imports["imports"][util.sha256_file(source_md)] = {
        "source": source_md.as_posix(),
        "dest": f"content/{rel_dest}/full.md",
        "when": util.now_iso(),
        "kind": "manual",
    }
    _save_imports(imports)
    return {"imported": f"content/{rel_dest}/full.md", "images": copied_images}


def import_directory(
    source_dir: Path,
    dest_dir: Path,
    *,
    collection: str,
    id_prefix: str,
    base_tags: list[str],
    source_note: str,
) -> dict[str, Any]:
    """Copy a directory tree, adding frontmatter to markdown files."""
    if not source_dir.exists():
        raise FileNotFoundError(source_dir)
    used: set[str] = set()
    for existing in store.load_docs():
        used.add(existing.id)
    files = 0
    markdown = 0
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_dir)
        if any(part in util.SKIP_DIRS for part in relative.parts):
            continue
        target = dest_dir / relative
        if source.suffix.lower() in {".md", ".markdown"}:
            rel_key = relative.as_posix()
            doc_id = _derive_id(id_prefix, rel_key, used)
            text = util.read_text(source)
            title = _title_from_text(text, source.stem)
            tags = list(base_tags) + _group_tags(rel_key)
            payload = prepare_markdown(
                text,
                doc_id=doc_id,
                title=title,
                collection=collection,
                tags=tags,
                source=f"{source_note}/{rel_key}",
                updated=datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y-%m-%d"),
            )
            util.write_text(target, payload)
            markdown += 1
            imports = _load_imports()
            imports["imports"][util.sha256_file(source)] = {
                "source": source.as_posix(),
                "dest": f"content/{target.relative_to(config.CONTENT).as_posix()}",
                "when": util.now_iso(),
                "kind": "markdown",
            }
            _save_imports(imports)
        else:
            _copy_asset(source, target)
        files += 1
    return {"files": files, "markdown": markdown, "dest": f"content/{dest_dir.relative_to(config.CONTENT).as_posix()}"}


def import_single_file(
    source: Path,
    dest_dir: Path,
    *,
    collection: str,
    doc_id: str,
    title: str | None,
    tags: list[str],
    source_note: str,
) -> dict[str, Any]:
    target = dest_dir / source.name
    util.write_text(target, util.read_text(source)) if source.suffix.lower() in {".md", ".markdown"} else _copy_asset(source, target)
    if source.suffix.lower() in {".md", ".markdown"}:
        text = util.read_text(source)
        payload = prepare_markdown(
            text,
            doc_id=doc_id,
            title=title or _title_from_text(text, source.stem),
            collection=collection,
            tags=tags,
            source=source_note,
        )
        util.write_text(target, payload)
    imports = _load_imports()
    imports["imports"][util.sha256_file(source)] = {
        "source": source.as_posix(),
        "dest": f"content/{target.relative_to(config.CONTENT).as_posix()}",
        "when": util.now_iso(),
        "kind": "file",
    }
    _save_imports(imports)
    return {"imported": f"content/{target.relative_to(config.CONTENT).as_posix()}"}


def new_doc(collection: str, doc_id: str, title: str, tags: list[str] | None = None) -> dict[str, Any]:
    dest = config.CONTENT / collection / f"{doc_id}.md"
    if dest.exists():
        raise FileExistsError(dest)
    payload = prepare_markdown(
        f"# {title}\n\n（在这里写正文）\n",
        doc_id=doc_id,
        title=title,
        collection=collection,
        tags=tags or [],
        source="kb new",
    )
    util.write_text(dest, payload)
    return {"created": f"content/{dest.relative_to(config.CONTENT).as_posix()}"}


def move_doc(ref: str, dest: str) -> dict[str, Any]:
    doc = store.resolve(ref)
    if doc is None or doc.abs_path is None:
        raise FileNotFoundError(f"document not found: {ref}")
    dest_path = Path(dest.replace("\\", "/"))
    if not dest_path.is_absolute():
        dest_path = config.CONTENT / dest_path
    if dest_path.exists() and dest_path.is_dir():
        dest_path = dest_path / doc.abs_path.name
    if dest_path.suffix == "":
        dest_path = dest_path.with_suffix(doc.abs_path.suffix)
    new_abs = dest_path.resolve()
    try:
        new_abs.relative_to(config.CONTENT.resolve())
    except ValueError as exc:
        raise ValueError("destination must stay inside content/") from exc
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(doc.abs_path), str(new_abs))
    if new_abs.suffix.lower() in {".md", ".markdown"}:
        text = util.read_text(new_abs)
        meta, body = frontmatter.split(text)
        if meta is None:
            meta = {"id": doc.id, "title": doc.title}
        aliases = meta.get("aliases") or []
        if doc.path not in aliases:
            aliases.append(doc.path)
        meta["aliases"] = aliases
        util.write_text(new_abs, frontmatter.dumps(meta, body))
    return {"moved": doc.path, "to": new_abs.relative_to(config.ROOT).as_posix()}


def delete_doc(ref: str) -> dict[str, Any]:
    doc = store.resolve(ref)
    if doc is None or doc.abs_path is None:
        raise FileNotFoundError(f"document not found: {ref}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.TRASH / stamp / doc.abs_path.relative_to(config.CONTENT)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(doc.abs_path), str(target))
    return {"deleted": doc.path, "trash": target.relative_to(config.ROOT).as_posix()}


def generate_toc() -> list[str]:
    """Regenerate _index.md for each top-level collection directory."""
    docs = [doc for doc in store.load_docs() if doc.status != "archived"]
    written = []
    top_dirs: set[str] = set()
    for doc in docs:
        parts = Path(doc.path).parts
        if len(parts) > 2:
            top_dirs.add(parts[1])
    for top in sorted(top_dirs):
        if top.startswith("."):
            continue
        index_target = config.CONTENT / top / "_index.md"
        lines = [f"# {top} 目录（自动生成）", "", "> 由 `kb toc` 生成，请勿手改；内容变更请编辑对应文档。", ""]
        current_dir = None
        for doc in sorted((d for d in docs if Path(d.path).parts[1] == top), key=lambda d: d.path):
            directory = Path(doc.path).parent.as_posix()
            if directory != current_dir:
                current_dir = directory
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"## {directory.replace('content/', '')}")
                lines.append("")
            relative = Path(doc.path).relative_to(Path("content") / top).as_posix()
            lines.append(f"- [{doc.title}]({relative}) — `{doc.id}`")
        lines.append("")
        util.write_text(index_target, "\n".join(lines))
        written.append(index_target.relative_to(config.ROOT).as_posix())
    return written
