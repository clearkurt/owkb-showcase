"""Retrieval evaluation for the OWKB knowledge base.

Run `kb eval` (or `python scripts/eval_search.py`) after changing content,
tokenization or ranking. The report is written to logs/eval_report.{md,json}.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from . import config, index, store, util

DEFAULT_QUERIES = config.ROOT / "evals" / "queries.yaml"
REPORT_MD = config.LOGS / "eval_report.md"
REPORT_JSON = config.LOGS / "eval_report.json"


def load_queries(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or DEFAULT_QUERIES
    if not path.exists():
        raise FileNotFoundError(f"evaluation set not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = payload.get("queries") or []
    if not items:
        raise ValueError(f"no queries in {path}")
    return items


def run(queries_path: Path | None = None, limit: int = 5) -> dict[str, Any]:
    config.ensure_dirs()
    queries = load_queries(queries_path)
    known_ids = {doc.id for doc in store.load_docs()}
    results: list[dict[str, Any]] = []
    for item in queries:
        query = str(item["query"])
        expect = [str(doc_id) for doc_id in item.get("expect_docs", [])]
        pages = [str(page) for page in item.get("expect_pages", [])]
        path_prefix = item.get("path_prefix")
        started = time.perf_counter()
        response = index.search(query, limit=limit, path_prefix=path_prefix)
        elapsed_ms = (time.perf_counter() - started) * 1000
        hits = response.get("hits", [])
        top = [{"id": hit["id"], "page": hit.get("page"), "title": hit.get("title")} for hit in hits]
        rank = 0
        for position, hit in enumerate(hits, start=1):
            if hit["id"] in expect:
                rank = position
                break
        top1 = bool(hits) and hits[0]["id"] in expect
        top3 = any(hit["id"] in expect for hit in hits[:3])
        page_ok = None
        if pages:
            page_ok = False
            for hit in hits:
                if hit["id"] in expect:
                    page_ok = str(hit.get("page") or "") in pages
                    break
        unknown = [doc_id for doc_id in expect if doc_id not in known_ids]
        results.append(
            {
                "id": item.get("id") or query,
                "query": query,
                "expect_docs": expect,
                "expect_pages": pages,
                "path_prefix": path_prefix,
                "top": top,
                "top1": top1,
                "top3": top3,
                "page_ok": page_ok,
                "rank": rank,
                "mrr": round(1.0 / rank, 4) if rank else 0.0,
                "elapsed_ms": round(elapsed_ms, 2),
                "unknown_expect_ids": unknown,
                "error": response.get("error"),
            }
        )

    total = len(results)
    top1_count = sum(1 for item in results if item["top1"])
    top3_count = sum(1 for item in results if item["top3"])
    page_items = [item for item in results if item["page_ok"] is not None]
    page_count = sum(1 for item in page_items if item["page_ok"])
    summary = {
        "total": total,
        "top1": top1_count,
        "top1_rate": round(top1_count / total, 4) if total else 0.0,
        "top3": top3_count,
        "top3_rate": round(top3_count / total, 4) if total else 0.0,
        "page_total": len(page_items),
        "page_ok": page_count,
        "page_rate": round(page_count / len(page_items), 4) if page_items else None,
        "mrr": round(sum(item["mrr"] for item in results) / total, 4) if total else 0.0,
        "avg_ms": round(sum(item["elapsed_ms"] for item in results) / total, 2) if total else 0.0,
        "index": index.stats(),
        "generated_at": util.now_iso(),
    }
    report = {"summary": summary, "results": results}
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    return {"summary": summary, "results": results, "report_md": str(REPORT_MD), "report_json": str(REPORT_JSON)}


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# OWKB 检索评测报告",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 索引：{summary['index'].get('docs', '?')} 篇 / {summary['index'].get('chunks', '?')} 块（built_at {summary['index'].get('built_at', '?')}）",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| Top-1 命中 | {summary['top1']}/{summary['total']}（{summary['top1_rate']:.1%}） |",
        f"| Top-3 命中 | {summary['top3']}/{summary['total']}（{summary['top3_rate']:.1%}） |",
    ]
    if summary.get("page_rate") is not None:
        lines.append(f"| 页码命中 | {summary['page_ok']}/{summary['page_total']}（{summary['page_rate']:.1%}） |")
    lines.extend(
        [
            f"| MRR | {summary['mrr']:.4f} |",
            f"| 平均耗时 | {summary['avg_ms']:.2f} ms |",
            "",
            "## 明细",
            "",
            "| # | 查询 | Top-1 | 命中文档 / 页码 | Top1 | Top3 | 页码 | 耗时(ms) |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in report["results"]:
        first = item["top"][0] if item["top"] else None
        hit = f"{first['id']} / p{first['page']}" if first else "（无命中）"
        lines.append(
            "| {id} | {query} | {hit} | {top1} | {top3} | {page} | {ms} |".format(
                id=item["id"],
                query=item["query"].replace("|", "\\|"),
                hit=hit,
                top1="✅" if item["top1"] else "❌",
                top3="✅" if item["top3"] else "❌",
                page="—" if item["page_ok"] is None else ("✅" if item["page_ok"] else "❌"),
                ms=item["elapsed_ms"],
            )
        )
    failures = [item for item in report["results"] if not item["top1"] or item["page_ok"] is False]
    lines.extend(["", "## 未命中 / 需关注", ""])
    if failures:
        for item in failures:
            expected = ", ".join(item["expect_docs"]) or "（未配置）"
            top = ", ".join(f"{hit['id']} p{hit['page']}" for hit in item["top"][:3]) or "（无命中）"
            if not item["top3"]:
                kind = "Top-3 未命中"
            elif not item["top1"]:
                kind = "Top-1 未命中（Top-3 已命中）"
            else:
                kind = "页码未命中"
            lines.append(f"- **{item['id']}** `{item['query']}`（{kind}）：期望 `{expected}`；实际 Top-3：{top}")
    else:
        lines.append("无。")
    return "\n".join(lines) + "\n"


def format_cli_summary(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        f"Top-1 {summary['top1']}/{summary['total']} ({summary['top1_rate']:.1%}) | "
        f"Top-3 {summary['top3']}/{summary['total']} ({summary['top3_rate']:.1%}) | "
        f"MRR {summary['mrr']:.4f} | avg {summary['avg_ms']:.1f} ms",
        f"report: {result['report_md']}",
    ]
    failures = [item for item in result["results"] if not item["top1"] or item["page_ok"] is False]
    for item in failures:
        top = ", ".join(f"{hit['id']}:p{hit['page']}" for hit in item["top"][:3]) or "无命中"
        kind = "FAIL" if not item["top3"] else ("PAGE" if item["page_ok"] is False else "MISS")
        lines.append(f"{kind} {item['id']} {item['query']} -> {top}")
    return "\n".join(lines)
