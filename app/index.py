"""SQLite FTS5 index build and search (Chinese via jieba segmentation)."""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from . import config, extract, frontmatter, store, util

# Guards build() against concurrent search()/stats() (auto-index watcher + requests).
_LOCK = threading.RLock()


def _locked(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return func(*args, **kwargs)

    return wrapper

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE docs (
    id TEXT PRIMARY KEY,
    path TEXT,
    title TEXT,
    type TEXT,
    collection TEXT,
    tags TEXT,
    status TEXT,
    updated TEXT
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    doc_id TEXT,
    kind TEXT,
    page_start INTEGER,
    page_end INTEGER,
    page_label TEXT,
    heading TEXT,
    line_start INTEGER,
    line_end INTEGER,
    text TEXT
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    title, heading, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.INDEX_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def is_built() -> bool:
    return config.INDEX_FILE.exists()


@_locked
def build() -> dict[str, Any]:
    """Full rebuild: scan content, chunk every document, rebuild SQLite index."""
    docs, orphans = store.scan()
    config.DATA.mkdir(parents=True, exist_ok=True)
    if config.INDEX_FILE.exists():
        config.INDEX_FILE.unlink()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        chunk_count = 0
        doc_rows = []
        for doc in docs:
            if doc.abs_path is None:
                continue
            doc_rows.append(
                (
                    doc.id,
                    doc.path,
                    doc.title,
                    doc.type,
                    doc.collection,
                    json.dumps(doc.tags, ensure_ascii=False),
                    doc.status,
                    doc.updated,
                )
            )
            text = util.read_text(doc.abs_path)
            is_md = doc.abs_path.suffix.lower() in {".md", ".markdown"}
            body = text
            if is_md:
                _, body = frontmatter.split(text)
            for chunk in extract.chunks_for_text(body, is_md):
                conn.execute(
                    "INSERT INTO chunks(doc_id, kind, page_start, page_end, page_label, heading, line_start, line_end, text)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        doc.id,
                        chunk.kind,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.page_label,
                        chunk.heading,
                        chunk.line_start,
                        chunk.line_end,
                        chunk.text,
                    ),
                )
                rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, title, heading, body) VALUES (?,?,?,?)",
                    (
                        rowid,
                        extract.tokenize(doc.title),
                        extract.tokenize(chunk.heading),
                        extract.tokenize(chunk.text),
                    ),
                )
                chunk_count += 1
        conn.executemany(
            "INSERT INTO docs(id, path, title, type, collection, tags, status, updated)"
            " VALUES (?,?,?,?,?,?,?,?)",
            doc_rows,
        )
        meta = {
            "built_at": util.now_iso(),
            "docs": len(doc_rows),
            "chunks": chunk_count,
            "orphans": len(orphans),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
        conn.commit()
    finally:
        conn.close()
    return meta


def ensure_index() -> dict[str, Any]:
    if not is_built():
        return build()
    return stats()


@_locked
def stats() -> dict[str, Any]:
    if not is_built():
        return {"built": False}
    conn = connect()
    try:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        meta["built"] = True
        return meta
    finally:
        conn.close()


def _match_expression(query: str) -> tuple[str, list[str]]:
    terms = [term for term in extract.tokenize(query).split() if term.strip()]
    if not terms:
        return "", []
    quoted = []
    for term in terms:
        safe = term.replace('"', '""')
        quoted.append(f'"{safe}"')
    return " OR ".join(quoted), terms


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _code_identifier_tokens(query: str) -> list[str]:
    """Return exact identifier-like tokens (underscore/dot) for code search."""
    tokens = {
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", query)
        if "_" in token or "." in token
    }
    return sorted(tokens)


def _relevance_boost(query: str, doc: sqlite3.Row, tags: list[str], text: str = "") -> float:
    """Small rule-based boosts on top of BM25.

    Only boost signals that are unambiguous and useful in practice:
    - exact model token in title/path (M8P850 etc.)
    - RCC category when the query is clearly about RCC/long-wave timing
    - exact code identifier (`DO_INT_128HZ`, `lcd_map.inc`, ...) in code docs
    - demote code/demo docs for ordinary document queries
    """
    q = query.casefold()
    title = (doc["title"] or "").casefold()
    path = (doc["path"] or "").casefold()
    collection = (doc["collection"] or "").casefold()
    boost = 0.0
    for token in set(re.findall(r"m\d[a-z0-9]{2,}", query, re.IGNORECASE)):
        token = token.casefold()
        if token in title or token in path:
            boost += 2.0
    rcc_query = any(word in q for word in ("rcc", "长波", "授时", "dcf", "wwvb", "msf", "jjy"))
    if rcc_query and collection.startswith("写法/rcc"):
        boost += 2.0
    if rcc_query and not any(word in q for word in ("rf", "5103", "5116", "idt")) and collection.startswith("写法/rf"):
        boost -= 2.0
    if (doc["type"] or "") == "code":
        code_tokens = _code_identifier_tokens(query)
        if not code_tokens or _has_cjk(query):
            boost -= 1.2
        else:
            body = (text or "").casefold()
            for token in code_tokens:
                needle = token.casefold()
                if needle in title or needle in path:
                    boost += 3.0
                elif needle in body:
                    boost += 2.0
    return boost


@_locked
def search(
    query: str,
    limit: int = 5,
    collection: str | None = None,
    tag: str | None = None,
    path_prefix: str | None = None,
    include_drafts: bool = False,
) -> dict[str, Any]:
    if not query.strip():
        return {"query": query, "hits": [], "error": "empty query"}
    match, terms = _match_expression(query)
    if not match:
        return {"query": query, "hits": [], "error": "no searchable terms"}
    if not is_built():
        build()
    prefix = util.content_relative(path_prefix) if path_prefix else ""
    width = int(config.load_config()["index"].get("snippet_width", 180))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT f.rowid AS chunk_id, bm25(chunks_fts, 4.0, 2.5, 1.0) AS score FROM chunks_fts f"
            " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, max(limit * 8, 40)),
        ).fetchall()
        candidates: list[tuple[int, float, float, sqlite3.Row, sqlite3.Row, list[str]]] = []
        candidate_chunk_ids: set[int] = set()
        for order, row in enumerate(rows):
            chunk = conn.execute("SELECT * FROM chunks WHERE id = ?", (row["chunk_id"],)).fetchone()
            if chunk is None:
                continue
            doc = conn.execute("SELECT * FROM docs WHERE id = ?", (chunk["doc_id"],)).fetchone()
            if doc is None:
                continue
            tags = json.loads(doc["tags"] or "[]")
            if not include_drafts and doc["status"] != "published":
                continue
            if collection and not doc["collection"].startswith(collection):
                continue
            if tag and tag not in tags:
                continue
            if prefix:
                doc_path = doc["path"]
                if doc_path.startswith("content/"):
                    doc_path = doc_path[len("content/"):]
                if not doc_path.startswith(prefix):
                    continue
            base = -float(row["score"])
            boost = _relevance_boost(query, doc, tags, chunk["text"])
            candidates.append((order, base + boost, base, chunk, doc, tags))
            candidate_chunk_ids.add(int(chunk["id"]))

        # Code identifiers are tokenised by jieba (DO_INT_128HZ -> DO/_/INT/_/128HZ),
        # so FTS can miss them. Add exact substring candidates from code docs.
        code_tokens = _code_identifier_tokens(query)
        if code_tokens and not _has_cjk(query):
            next_order = len(rows)
            for token in code_tokens:
                pattern = f"%{token}%"
                exact_rows = conn.execute(
                    "SELECT c.id AS chunk_id FROM chunks c JOIN docs d ON d.id = c.doc_id"
                    " WHERE d.type = 'code' AND (c.text LIKE ? OR d.path LIKE ? OR d.title LIKE ?)"
                    " LIMIT 20",
                    (pattern, pattern, pattern),
                ).fetchall()
                for exact in exact_rows:
                    chunk_id = int(exact["chunk_id"])
                    if chunk_id in candidate_chunk_ids:
                        continue
                    chunk = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                    if chunk is None:
                        continue
                    doc = conn.execute("SELECT * FROM docs WHERE id = ?", (chunk["doc_id"],)).fetchone()
                    if doc is None:
                        continue
                    tags = json.loads(doc["tags"] or "[]")
                    if not include_drafts and doc["status"] != "published":
                        continue
                    if collection and not doc["collection"].startswith(collection):
                        continue
                    if tag and tag not in tags:
                        continue
                    if prefix:
                        doc_path = doc["path"]
                        if doc_path.startswith("content/"):
                            doc_path = doc_path[len("content/"):]
                        if not doc_path.startswith(prefix):
                            continue
                    needle = token.casefold()
                    in_path = needle in (doc["path"] or "").casefold() or needle in (doc["title"] or "").casefold()
                    score = 103.0 if in_path else 100.0
                    candidates.append((next_order, score, score - 1.0, chunk, doc, tags))
                    candidate_chunk_ids.add(chunk_id)
                    next_order += 1

        # Keep FTS5 order when no boost fired (cheap + preserves current ranking);
        # only re-sort when at least one candidate actually received a boost.
        if any(candidate[1] != candidate[2] for candidate in candidates):
            candidates.sort(key=lambda item: (-item[1], item[0]))
        hits: list[dict[str, Any]] = []
        per_doc: Counter[str] = Counter()
        for _order, score, _base, chunk, doc, tags in candidates:
            if per_doc[doc["id"]] >= 4:
                continue
            per_doc[doc["id"]] += 1
            hits.append(
                {
                    "id": doc["id"],
                    "title": doc["title"],
                    "path": doc["path"],
                    "doc_type": doc["type"],
                    "collection": doc["collection"],
                    "tags": tags,
                    "page": chunk["page_label"] or None,
                    "heading": chunk["heading"] or None,
                    "line_start": chunk["line_start"],
                    "line_end": chunk["line_end"],
                    "score": round(score, 3),
                    "snippet": extract.snippet(chunk["text"], terms, width),
                }
            )
            if len(hits) >= limit:
                break
        return {"query": query, "hits": hits, "terms": terms}
    finally:
        conn.close()


@_locked
def all_tags() -> list[dict[str, Any]]:
    if not is_built():
        build()
    conn = connect()
    try:
        counter: Counter[str] = Counter()
        for row in conn.execute("SELECT tags FROM docs"):
            for tag in json.loads(row["tags"] or "[]"):
                counter[tag] += 1
        return [{"tag": tag, "count": count} for tag, count in counter.most_common()]
    finally:
        conn.close()


def health() -> dict[str, Any]:
    meta = stats()
    return {
        "ok": True,
        "service": "owkb",
        "version": "0.1.0",
        "index": meta,
        "content_root": str(config.CONTENT),
    }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
