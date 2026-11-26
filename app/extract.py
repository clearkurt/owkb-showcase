"""Text extraction, chunking (markdown / page-anchored manuals / code) and snippets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PAGE_RE = re.compile(r"<!--\s*pdf-pages?:\s*(\d+)(?:\s*-\s*(\d+))?\s*-->")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
MAX_CHUNK_CHARS = 4500

_jieba = None


def jieba():
    global _jieba
    if _jieba is None:
        import warnings

        warnings.filterwarnings("ignore", message="pkg_resources is deprecated.*")
        import jieba as _j

        _j.setLogLevel(40)
        _jieba = _j
    return _jieba


def tokenize(text: str) -> str:
    """Chinese-aware segmentation for FTS5 index terms."""
    tokens = []
    for token in jieba().lcut_for_search(text):
        token = token.strip()
        if token and not token.isspace():
            tokens.append(token)
    return " ".join(tokens)


def is_manual_text(text: str) -> bool:
    return PAGE_RE.search(text) is not None


@dataclass
class Chunk:
    kind: str
    page_start: int
    page_end: int
    page_label: str
    heading: str
    line_start: int
    line_end: int
    text: str


def _pages(text: str) -> list[Chunk]:
    matches = list(PAGE_RE.finditer(text))
    if not matches:
        return []
    chunks: list[Chunk] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[start:end].strip("\n")
        if not segment.strip():
            continue
        page_start = int(match.group(1))
        page_end = int(match.group(2)) if match.group(2) else page_start
        heading_match = re.search(r"^#{1,6}\s+(.+?)\s*$", segment, re.MULTILINE)
        heading = heading_match.group(1).strip() if heading_match else ""
        line_start = text.count("\n", 0, start) + 1
        line_end = text.count("\n", 0, end)
        label = f"{page_start}" if page_start == page_end else f"{page_start}-{page_end}"
        chunks.append(
            Chunk("page", page_start, page_end, label, heading, line_start, line_end, segment)
        )
    return chunks


def _split_long(text: str, line_start: int) -> list[tuple[str, int, int]]:
    """Split an over-long chunk on blank lines / newlines, keeping line ranges."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [(text, line_start, line_start + text.count("\n"))]
    pieces: list[tuple[str, int, int]] = []
    lines = text.split("\n")
    current: list[str] = []
    current_start = line_start
    size = 0
    for offset, line in enumerate(lines):
        current.append(line)
        size += len(line) + 1
        if size >= MAX_CHUNK_CHARS:
            pieces.append(("\n".join(current), current_start, current_start + len(current) - 1))
            current_start = line_start + offset + 1
            current = []
            size = 0
    if current:
        pieces.append(("\n".join(current), current_start, current_start + len(current) - 1))
    return pieces


def chunk_markdown(body: str, base_line: int = 1) -> list[Chunk]:
    """Split markdown into heading based sections (fallback: long paragraphs)."""
    headings = [(m.start(), m.group(2).strip()) for m in HEAD_RE.finditer(body)]
    line_starts = [0]
    for match in re.finditer("\n", body):
        line_starts.append(match.end())

    def line_of(offset: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    sections: list[tuple[str, str, int, int]] = []  # heading, text, first_line, last_line
    if not headings:
        for piece, start, end in _split_long(body, base_line):
            sections.append(("", piece, start, end))
    else:
        if headings[0][0] > 0:
            prefix = body[: headings[0][0]]
            if prefix.strip():
                sections.append(("", prefix, base_line, base_line + prefix.count("\n")))
        for index, (offset, heading) in enumerate(headings):
            end = headings[index + 1][0] if index + 1 < len(headings) else len(body)
            segment = body[offset:end]
            first = line_of(offset) + base_line - 1
            for piece, start, finish in _split_long(segment, first):
                sections.append((heading, piece, start, finish))
    chunks = []
    for heading, text, first, last in sections:
        if not text.strip():
            continue
        chunks.append(Chunk("section", 0, 0, "", heading, first, last, text.strip("\n")))
    return chunks


def chunks_for_text(text: str, is_markdown: bool) -> list[Chunk]:
    manual = _pages(text)
    if manual:
        return manual
    if is_markdown:
        return chunk_markdown(text)
    return [Chunk("block", 0, 0, "", "", 1, text.count("\n") + 1, text)]


def extract_pdf_pages(path: Path) -> list[tuple[int, str]]:
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    pages = []
    for index in range(doc.page_count):
        pages.append((index + 1, doc[index].get_text()))
    doc.close()
    return pages


def snippet(text: str, terms: list[str], width: int = 180) -> str:
    """Return a readable excerpt around the first term occurrence."""
    compact = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[\1]", text)
    compact = re.sub(r"<[^>]+>", " ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return ""
    lowered = compact.casefold()
    positions = []
    for term in terms:
        term = term.strip()
        if not term:
            continue
        found = lowered.find(term.casefold())
        if found != -1:
            positions.append(found)
    position = min(positions) if positions else 0
    start = max(0, position - width // 3)
    end = min(len(compact), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def image_refs(text: str) -> list[dict[str, str]]:
    out = []
    for match in IMAGE_RE.finditer(text):
        alt, src = match.group(1), match.group(2)
        if src.startswith("http://") or src.startswith("https://"):
            continue
        out.append({"alt": alt, "src": src})
    return out
