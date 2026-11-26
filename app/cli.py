"""`kb` command line tool for maintaining the OWKB knowledge base."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config, importer, index, store, tokens, util

PAGE_COUNT_RE = None


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _ensure_init() -> None:
    config.ensure_dirs()
    util.ensure_git_identity()


def _tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]


def _commit(message: str) -> None:
    rev = util.git_commit(message)
    if rev:
        print(f"git commit: {rev}")


def cmd_init(_args) -> None:
    _ensure_init()
    if not (config.ROOT / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=config.ROOT, capture_output=True, text=True)
    util.ensure_git_identity()
    _print({"ok": True, "root": str(config.ROOT)})


def cmd_import(args) -> None:
    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"source not found: {source}")
    config.ensure_dirs()
    tags = _tags(args.tags)
    source_note = args.source_note or source.as_posix()
    collection = args.collection.replace("\\", "/").strip("/")
    if args.manual:
        from . import extract

        text = util.read_text(source)
        page_count = args.pages
        if page_count is None:
            page_count = len(extract.PAGE_RE.findall(text))
        dest_dir = config.CONTENT / collection / args.dest if args.dest else config.CONTENT / collection
        result = importer.import_manual(
            source,
            Path(args.snapshots).expanduser().resolve() if args.snapshots else None,
            dest_dir,
            doc_id=args.id,
            title=args.title or source.stem,
            collection=collection,
            tags=tags,
            page_count=page_count,
            pdf_source=Path(args.pdf).expanduser().resolve() if args.pdf else None,
            pdf_library_rel=args.pdf_library or "",
            source_note=source_note,
        )
    elif source.is_dir():
        dest_dir = config.CONTENT / collection
        result = importer.import_directory(
            source,
            dest_dir,
            collection=collection,
            id_prefix=args.id_prefix or util.slug_id(Path(collection).name),
            base_tags=tags,
            source_note=source_note,
        )
    else:
        if not args.id:
            raise SystemExit("--id is required when importing a single file")
        dest_dir = config.CONTENT / collection
        result = importer.import_single_file(
            source,
            dest_dir,
            collection=collection,
            doc_id=args.id,
            title=args.title,
            tags=tags,
            source_note=source_note,
        )
    meta = index.build()
    _print({"result": result, "index": meta})
    _commit(f"kb-ops: import {source.name}")


def cmd_new(args) -> None:
    config.ensure_dirs()
    result = importer.new_doc(args.collection, args.id, args.title, _tags(args.tags))
    index.build()
    _print(result)
    _commit(f"kb-ops: new document {args.id}")


def cmd_reindex(_args) -> None:
    meta = index.build()
    _print(meta)
    if not _args.no_commit:
        _commit("kb-ops: reindex")


def cmd_status(_args) -> None:
    meta = index.stats()
    docs = store.load_docs()
    by_type: dict[str, int] = {}
    by_collection: dict[str, int] = {}
    for doc in docs:
        by_type[doc.type] = by_type.get(doc.type, 0) + 1
        by_collection[doc.collection] = by_collection.get(doc.collection, 0) + 1
    last_commit = util.git(["log", "-1", "--format=%h %s"]).stdout.strip() if util.git_available() else ""
    _print(
        {
            "root": str(config.ROOT),
            "index": meta,
            "documents": len(docs),
            "by_type": by_type,
            "by_collection": by_collection,
            "last_commit": last_commit,
        }
    )


def cmd_check(_args) -> None:
    docs = store.load_docs()
    issues: list[dict[str, Any]] = []
    paths = {doc.path for doc in docs}
    for doc in docs:
        if doc.abs_path is None:
            issues.append({"level": "error", "kind": "missing_file", "id": doc.id, "path": doc.path})
    seen_hashes: dict[str, str] = {}
    for doc in docs:
        if doc.abs_path is None or not doc.hash:
            continue
        previous = seen_hashes.get(doc.hash)
        if previous and previous != doc.id:
            issues.append({"level": "warn", "kind": "duplicate_content", "id": doc.id, "with": previous})
        else:
            seen_hashes[doc.hash] = doc.id
    catalog = store.load_catalog()
    for alias, doc_id in catalog.get("aliases", {}).items():
        if doc_id not in catalog.get("docs", {}):
            issues.append({"level": "warn", "kind": "stale_alias", "alias": alias, "id": doc_id})
    known = {doc.path for doc in docs}
    for path in config.CONTENT.rglob("*"):
        if not path.is_file() or not util.is_text_file(path):
            continue
        rel = path.relative_to(config.ROOT).as_posix()
        if path.name in util.SKIP_NAMES or any(part in util.SKIP_DIRS for part in path.parts):
            continue
        if rel not in known:
            issues.append({"level": "warn", "kind": "not_indexed", "path": rel})
    meta = index.stats()
    _print({"documents": len(docs), "paths": len(paths), "index": meta, "issues": issues})
    if any(issue["level"] == "error" for issue in issues):
        sys.exit(1)


def cmd_toc(_args) -> None:
    written = importer.generate_toc()
    _print({"written": written})
    _commit("kb-ops: regenerate _index.md")


def cmd_mv(args) -> None:
    result = importer.move_doc(args.ref, args.dest)
    index.build()
    _print(result)
    _commit(f"kb-ops: move {args.ref} -> {args.dest}")


def cmd_rm(args) -> None:
    result = importer.delete_doc(args.ref)
    index.build()
    _print(result)
    _commit(f"kb-ops: remove {args.ref}")


def cmd_token(args) -> None:
    action = args.action
    if action == "list":
        _print({"tokens": tokens.list_tokens()})
        return
    if action == "enroll-code":
        code = tokens.rotate_enroll_code() if args.rotate else tokens.ensure_enroll_code()
        _print({"enroll_code": code, "file": str(config.ENROLL_PLAIN_FILE)})
        return
    if action == "add":
        entry, token = tokens.create_token(
            args.name,
            note=args.note or "",
            device_id=args.device_id or "",
            device_name=args.device_name or "",
        )
        config.TOKEN_PLAIN_FILE.write_text(token + "\n", encoding="utf-8")
        _print({"name": entry["name"], "token": token, "note": entry.get("note", ""), "file": str(config.TOKEN_PLAIN_FILE)})
        return
    if action == "bind":
        if not args.device_id:
            raise SystemExit("--device-id is required for token bind")
        try:
            entry = tokens.bind_token_device(args.name, args.device_id, args.device_name or "")
        except tokens.TokenError as exc:
            raise SystemExit(str(exc)) from exc
        if entry is None:
            raise SystemExit(f"token not found: {args.name}")
        _print({"bound": args.name, "device_id": entry.get("device_id", ""), "device_name": entry.get("device_name", "")})
        return
    if action == "unbind":
        entry = tokens.unbind_token_device(args.name)
        if entry is None:
            raise SystemExit(f"token not found: {args.name}")
        _print({"unbound": args.name})
        return
    if action == "rotate":
        result = tokens.rotate_token(args.name)
        if result is None:
            raise SystemExit(f"token not found: {args.name}")
        entry, token = result
        config.TOKEN_PLAIN_FILE.write_text(token + "\n", encoding="utf-8")
        _print({"name": entry["name"], "token": token, "file": str(config.TOKEN_PLAIN_FILE)})
        return
    if action == "delete":
        if not tokens.delete_token(args.name):
            raise SystemExit(f"token not found: {args.name}")
        _print({"deleted": args.name})
        return
    raise SystemExit(f"unknown token action: {action}")


def cmd_uploads(args) -> None:
    from . import notify, upload_import, uploads

    action = args.upload_action
    if action == "list":
        _print({"uploads": uploads.list_uploads()})
        return
    if action == "show":
        _print(uploads.get_upload(args.upload_id))
        return
    if action == "approve":
        try:
            result = upload_import.approve(
                args.upload_id,
                target=args.target or "",
                title=args.title or "",
                tags=_tags(args.tags),
                replace=args.replace,
                pdf_library=args.pdf_library or "",
            )
        except upload_import.ApproveError as exc:
            raise SystemExit(str(exc)) from exc
        _print(result)
        return
    if action == "reject":
        try:
            manifest = uploads.review(args.upload_id, "rejected", args.note or "")
        except uploads.UploadError as exc:
            raise SystemExit(str(exc)) from exc
        _print({"id": manifest["id"], "status": manifest["status"], "reviewed": manifest.get("reviewed")})
        return
    if action == "notify":
        _print(notify.send_pending(limit=args.limit))
        return
    if action == "prune":
        try:
            statuses = tuple(item.strip() for item in (args.status or "approved,rejected").split(",") if item.strip())
            _print(uploads.prune(days=args.days, statuses=statuses, dry_run=args.dry_run))
        except uploads.UploadError as exc:
            raise SystemExit(str(exc)) from exc
        return
    if action == "audit-rotate":
        from . import audit

        _print(audit.rotate(max_bytes=int(args.max_mb * 1024 * 1024), keep=args.keep))
        return
    raise SystemExit(f"unknown upload action: {action}")


def cmd_alerts(args) -> None:
    from . import health_alerts

    _print(health_alerts.check(dry_run=args.dry_run, force=args.force))


def cmd_serve(args) -> None:
    from . import server

    server.serve(host=args.host, port=args.port)


def cmd_backup(_args) -> None:
    backups = config.ROOT / "backups"
    backups.mkdir(exist_ok=True)
    stamp = util.now_iso().replace(":", "").replace("+08:00", "").replace("-", "")
    if util.git_available():
        bundle = backups / f"owkb-{stamp}.bundle"
        result = util.git(["bundle", "create", str(bundle), "--all"])
        if result.returncode != 0:
            raise SystemExit(result.stderr)
        _print({"bundle": bundle.relative_to(config.ROOT).as_posix(), "size": bundle.stat().st_size})
    else:
        archive = shutil.make_archive(str(backups / f"owkb-{stamp}"), "zip", root_dir=config.ROOT)
        _print({"archive": archive})


def cmd_commit(args) -> None:
    _commit(args.message)
    _print({"ok": True})


def cmd_eval(args) -> None:
    from . import evals

    result = evals.run(Path(args.queries), limit=args.limit)
    print(evals.format_cli_summary(result))
    summary = result["summary"]
    failed = False
    if args.min_top1 and summary["top1_rate"] < args.min_top1:
        print(f"FAIL: Top-1 {summary['top1_rate']:.1%} < {args.min_top1:.1%}", file=sys.stderr)
        failed = True
    if args.min_top3 and summary["top3_rate"] < args.min_top3:
        print(f"FAIL: Top-3 {summary['top3_rate']:.1%} < {args.min_top3:.1%}", file=sys.stderr)
        failed = True
    if failed:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb", description="OWKB knowledge base maintenance CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create folders and initialise git")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="import a file/directory/manual into content/")
    p.add_argument("source")
    p.add_argument("--collection", required=True, help="destination collection, relative to content/")
    p.add_argument("--dest", help="extra destination folder (manual mode), relative to collection")
    p.add_argument("--id", help="document id (single file/manual)")
    p.add_argument("--title")
    p.add_argument("--tags", help="comma separated")
    p.add_argument("--manual", action="store_true", help="import converted manual: full.md + snapshots")
    p.add_argument("--snapshots", help="snapshots directory for manual mode")
    p.add_argument("--pdf", help="original PDF to archive in library/")
    p.add_argument("--pdf-library", help="library-relative destination for the PDF")
    p.add_argument("--pages", type=int, help="page count for manual mode")
    p.add_argument("--id-prefix", help="prefix for generated ids when importing a directory")
    p.add_argument("--source-note", help="source field written to frontmatter")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("new", help="create a new markdown document")
    p.add_argument("collection")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--tags")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("reindex", help="rebuild the search index")
    p.add_argument("--no-commit", action="store_true")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("status", help="show kb status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", help="validate ids, paths, duplicates and index freshness")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("toc", help="regenerate _index.md files")
    p.set_defaults(func=cmd_toc)

    p = sub.add_parser("mv", help="move/rename a document (keeps id and records alias)")
    p.add_argument("ref", help="id, path or alias")
    p.add_argument("dest", help="new path relative to content/ (or a directory)")
    p.set_defaults(func=cmd_mv)

    p = sub.add_parser("rm", help="soft delete a document into _trash/")
    p.add_argument("ref")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("token", help="manage personal read-only bearer tokens")
    p.add_argument("action", choices=["add", "rotate", "delete", "list", "enroll-code", "bind", "unbind"])
    p.add_argument("--name", default="shared-readonly")
    p.add_argument("--note", default="")
    p.add_argument("--device-id", default="", help="device id for token add/bind")
    p.add_argument("--device-name", default="", help="human-readable device name")
    p.add_argument("--rotate", action="store_true", help="rotate the enrollment code (with action enroll-code)")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("uploads", help="review quarantined uploads / send notifications / prune")
    p.add_argument("upload_action", choices=["list", "show", "approve", "reject", "notify", "prune", "audit-rotate"])
    p.add_argument("upload_id", nargs="?", default="", help="upload id for show/approve/reject")
    p.add_argument("--note", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--target", default="", help="approve project: content-relative destination, e.g. 代码库/NAME; not used for PDF")
    p.add_argument("--title", default="", help="approve: project title")
    p.add_argument("--tags", default="", help="approve: comma separated tags")
    p.add_argument("--replace", action="store_true", help="approve: replace an existing target (old target moves to _trash/)")
    p.add_argument("--pdf-library", default="", help="approve PDF: library-relative destination, e.g. library/datasheets/VENDOR/MCU/file.pdf")
    p.add_argument("--days", type=int, default=30, help="prune: remove reviewed uploads older than N days")
    p.add_argument("--status", default="approved,rejected", help="prune: comma separated statuses (approved,rejected)")
    p.add_argument("--dry-run", action="store_true", help="prune: report without deleting")
    p.add_argument("--max-mb", type=float, default=10.0, help="audit-rotate: max active audit size in MB")
    p.add_argument("--keep", type=int, default=5, help="audit-rotate: number of archives to keep")
    p.set_defaults(func=cmd_uploads)

    p = sub.add_parser("alerts", help="check disk, quarantine, notification and service health")
    p.add_argument("action", choices=["check"])
    p.add_argument("--dry-run", action="store_true", help="report without sending email or changing state")
    p.add_argument("--force", action="store_true", help="bypass the cooldown")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("eval", help="run the retrieval evaluation set")
    p.add_argument("--queries", default=str(config.ROOT / "evals" / "queries.yaml"))
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--min-top1", type=float, default=0.0)
    p.add_argument("--min-top3", type=float, default=0.0)
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("serve", help="run the HTTP/MCP server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("backup", help="git bundle / zip backup")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("commit", help="git commit pending changes")
    p.add_argument("message", nargs="?", default="kb-ops: manual change")
    p.set_defaults(func=cmd_commit)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _ensure_init()
    args.func(args)


if __name__ == "__main__":
    main()
