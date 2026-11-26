"""Import admin-approved uploads into OWKB.

The upload endpoint only stores quarantined payloads.  This module is the
explicit admin-side bridge:

* project zip -> extract and import into ``content/代码库/`` plus a draft case card;
* PDF        -> archive the original only; PDF parsing is delegated to the
  external RAG parser pipeline.

It never parses PDF text, and it never lets an uploaded payload enter
``content/`` without an explicit approve.
"""

from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import config, importer, index, store, uploads, util

MAX_EXTRACT_BYTES = 500 * 1024 * 1024
MAX_EXTRACT_FILES = 5000
_SKIP_EXTRACT_NAMES = {"__MACOSX"}


class ApproveError(Exception):
    pass


def _tags(values: list[str] | None, extra: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for value in list(values or []) + list(extra or []):
        value = str(value).strip()
        if value and value not in out:
            out.append(value)
    return out


def _safe_slug(value: str, fallback: str = "upload") -> str:
    return util.slug_id(value, fallback) or fallback


def _resolve_target(value: str, default: str) -> tuple[str, Path]:
    rel = (value or default).replace("\\", "/").strip("/")
    if not rel:
        raise ApproveError("target is empty")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ApproveError(f"invalid content target: {value!r}")
    dest = (config.CONTENT / rel).resolve()
    try:
        dest.relative_to(config.CONTENT.resolve())
    except ValueError as exc:
        raise ApproveError("target must stay inside content/") from exc
    return rel, dest


def _prepare_target(dest: Path, upload_id: str, replace: bool) -> None:
    if dest.exists():
        if not replace:
            raise ApproveError(f"target already exists: {dest.relative_to(config.ROOT).as_posix()} (use --replace)")
        config.TRASH.mkdir(parents=True, exist_ok=True)
        backup = config.TRASH / f"{upload_id}-{dest.name}"
        if backup.exists():
            backup = config.TRASH / f"{upload_id}-{dest.name}-{__import__('uuid').uuid4().hex[:6]}"
        shutil.move(str(dest), str(backup))
    dest.mkdir(parents=True, exist_ok=False)


def _extract_zip(payload: Path, dest: Path) -> int:
    if not zipfile.is_zipfile(payload):
        raise ApproveError("project payload is not a valid zip file")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    total = 0
    count = 0
    with zipfile.ZipFile(payload) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if not name or name.endswith("/") or any(part in _SKIP_EXTRACT_NAMES for part in parts):
                continue
            if name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ApproveError(f"unsafe zip entry: {info.filename!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ApproveError(f"symlink in zip is not allowed: {info.filename!r}")
            count += 1
            if count > MAX_EXTRACT_FILES:
                raise ApproveError("zip contains too many files")
            total += int(info.file_size)
            if total > MAX_EXTRACT_BYTES:
                raise ApproveError("zip expands beyond the allowed size")
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if count == 0:
        raise ApproveError("zip contains no files")
    return count


def _unique_doc_id(base: str, upload_id: str) -> str:
    candidate = _safe_slug(base)
    if store.resolve(candidate) is None:
        return candidate
    candidate = f"{candidate}-{upload_id[-8:]}"
    if store.resolve(candidate) is None:
        return candidate
    return f"{candidate}-{__import__('uuid').uuid4().hex[:6]}"


def _write_project_card(
    *,
    upload_id: str,
    manifest: dict[str, Any],
    title: str,
    slug: str,
    target_rel: str,
    tags: list[str],
) -> str | None:
    case_slug = _safe_slug(slug)
    case_dir = config.CONTENT / "项目案例" / case_slug
    overview = case_dir / "00-项目概览.md"
    if overview.exists():
        return overview.relative_to(config.CONTENT).as_posix()
    case_dir.mkdir(parents=True, exist_ok=True)
    doc_id = _unique_doc_id(f"case-{case_slug}", upload_id)
    body = "\n".join(
        [
            f"# {title}",
            "",
            "> 由 OWKB 上传审核自动生成的项目卡，技术细节待管理员补充。",
            "",
            "## 来源",
            "",
            f"- 上传 ID：`{upload_id}`",
            f"- 上传人：`{manifest.get('uploader') or '-'}`",
            f"- 设备：`{manifest.get('device_name') or '-'}`",
            f"- 上传时间：`{manifest.get('created') or '-'}`",
            f"- 源码目录：`{target_rel}`",
            "",
            "## 待补充",
            "",
            "- 项目背景与产品定位",
            "- 关键模块与复用点",
            "- 构建/烧录方式",
            "- 已知限制与踩坑",
            "",
        ]
    )
    payload = importer.prepare_markdown(
        body,
        doc_id=doc_id,
        title=title,
        collection=f"项目案例/{case_slug}",
        tags=_tags(tags, ["项目案例", "上传入库"]),
        source=f"upload:{upload_id}",
        extra={"status": "draft"},
    )
    util.write_text(overview, payload)
    return overview.relative_to(config.CONTENT).as_posix()


def _pdf_library_rel(value: str, *, slug: str, filename: str) -> str:
    rel = (value or f"library/datasheets/{slug}/{filename}").replace("\\", "/").strip("/")
    if not rel.startswith("library/"):
        rel = f"library/{rel}"
    target = (config.ROOT / rel).resolve()
    try:
        target.relative_to(config.LIBRARY.resolve())
    except ValueError as exc:
        raise ApproveError("pdf library path must stay inside library/") from exc
    return rel


def _prepare_library_file(dest: Path, upload_id: str, replace: bool) -> None:
    if dest.exists():
        if not replace:
            raise ApproveError(f"PDF already exists: {dest.relative_to(config.ROOT).as_posix()} (use --replace)")
        config.TRASH.mkdir(parents=True, exist_ok=True)
        backup = config.TRASH / f"{upload_id}-{dest.name}"
        if backup.exists():
            backup = config.TRASH / f"{upload_id}-{dest.name}-{__import__('uuid').uuid4().hex[:6]}"
        shutil.move(str(dest), str(backup))
    dest.parent.mkdir(parents=True, exist_ok=True)


def _archive_pdf(payload: Path, dest: Path, upload_id: str, replace: bool) -> None:
    """Archive the original PDF only; do not parse or extract text."""
    if payload.read_bytes()[:5] != b"%PDF-":
        raise ApproveError("manual payload is not a PDF")
    _prepare_library_file(dest, upload_id, replace)
    shutil.copy2(payload, dest)


def approve(
    upload_id: str,
    *,
    target: str = "",
    title: str = "",
    tags: list[str] | None = None,
    replace: bool = False,
    pdf_library: str = "",
) -> dict[str, Any]:
    """Approve one pending upload.

    Project zips are imported into content; PDFs are archived as original
    files only for the external RAG parser.
    """
    try:
        manifest = uploads.get_upload(upload_id)
        if manifest.get("status") != "pending":
            raise ApproveError(f"upload is not pending: {manifest.get('status')}")
        payload = uploads.payload_path(upload_id)
    except uploads.UploadError as exc:
        raise ApproveError(str(exc)) from exc

    kind = str(manifest.get("kind") or "other")
    title = (title or str(manifest.get("name") or "")).strip() or Path(str(manifest.get("filename", "upload"))).stem
    slug = _safe_slug(title)
    tags = _tags(tags, ["上传入库", kind])
    work = uploads.upload_dir(upload_id)

    if kind == "project":
        target_rel, dest = _resolve_target(target, f"代码库/{slug}")
        _prepare_target(dest, upload_id, replace)
        extract_dir = work / "extracted"
        file_count = _extract_zip(payload, extract_dir)
        result = importer.import_directory(
            extract_dir,
            dest,
            collection=target_rel,
            id_prefix=_safe_slug(dest.name, "project"),
            base_tags=tags,
            source_note=f"upload:{upload_id}/{manifest.get('filename', 'payload')}",
        )
        case_rel = _write_project_card(
            upload_id=upload_id,
            manifest=manifest,
            title=title,
            slug=slug,
            target_rel=target_rel,
            tags=tags,
        )
        import_info = {
            "target": f"content/{target_rel}",
            "files": file_count,
            "result": result,
            "case": f"content/{case_rel}" if case_rel else None,
            "mode": "project",
        }
    elif kind == "manual":
        if target:
            raise ApproveError("manual approve archives the PDF only; use --pdf-library, not --target")
        library_rel = _pdf_library_rel(
            pdf_library,
            slug=_safe_slug(str(manifest.get("mcu") or title)),
            filename=str(manifest.get("filename") or payload.name),
        )
        library_dest = config.ROOT / library_rel
        _archive_pdf(payload, library_dest, upload_id, replace)
        import_info = {
            "target": library_rel,
            "pdf_library": library_rel,
            "mode": "manual-archive",
            "parsed": False,
            "next_step": "run the external RAG PDF parser on this file",
        }
    else:
        raise ApproveError(f"approve is not supported for upload kind: {kind}")

    if kind == "project":
        index_meta = index.build()
        note = f"imported to content/{target_rel}"
        commit = util.git_commit(f"kb-ops: approve upload {upload_id} -> {target_rel}")
    else:
        index_meta = None
        note = f"archived PDF to {library_rel}; parsing delegated to RAG parser"
        commit = None
    manifest = uploads.review(upload_id, "approved", note, extra={"import": import_info})
    return {
        "id": upload_id,
        "status": manifest["status"],
        "kind": kind,
        "title": title,
        "target": f"content/{target_rel}" if kind == "project" else library_rel,
        "import": import_info,
        "index": index_meta,
        "commit": commit,
    }
