"""MCP tool surface exposed to DSH agents (read-only)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import config, index, store, util

INSTRUCTIONS = """\
OWKB 是公司内部只读知识库（芯片手册、RCC/RF 写法、案例）。
使用顺序：先用 kb_search 找文档和页码/章节，再用 kb_read 读取命中位置；
需要按目录浏览用 kb_tree，按文件名匹配用 kb_glob，精确文本检索用 kb_grep。
回答时引用文档标题和路径（手册类带页码）。知识库只读，不要尝试写入。
图片/图表：kb_read 返回的 images 是图片路径，可用 kb_figure 取回图片内容。
"""


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def build_mcp() -> MCPServer:
    mcp = MCPServer(
        "owkb",
        title="Onlywell Knowledge Base",
        description="公司内部只读知识库：芯片手册、RCC/RF 写法与案例",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    @mcp.tool(
        name="kb_search",
        description="全文检索知识库。返回命中片段、文档路径、手册页码、标题与 id；limit 默认 5。"
        "path_prefix 可限定目录（如 芯片手册/磐芯/M8P850），collection/tag 可过滤集合与标签。",
    )
    def kb_search(
        query: str,
        limit: int = 5,
        collection: str | None = None,
        tag: str | None = None,
        path_prefix: str | None = None,
    ) -> str:
        if not query or not query.strip():
            return _json({"error": "query 不能为空"})
        return _json(
            index.search(
                query,
                limit=min(int(limit or 5), 20),
                collection=collection,
                tag=tag,
                path_prefix=path_prefix,
            )
        )

    @mcp.tool(
        name="kb_read",
        description="读取文档。参数：id 或 path 二选一；手册可加 page 读指定页；普通文档可用 start/end 行号。",
    )
    def kb_read(
        id: str | None = None,
        path: str | None = None,
        page: int | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> str:
        ref = id or path
        if not ref:
            return _json({"error": "需要提供 id 或 path"})
        doc = store.resolve(ref)
        if doc is None:
            return _json({"error": f"找不到文档: {ref}"})
        result = store.read(doc, page=page, start=start, end=end)
        base = _base_url()
        for image in result.get("images", []):
            image["url"] = f"{base}/api/download?path={image['path']}"
        return _json(result)

    @mcp.tool(name="kb_tree", description="浏览知识库目录树（path 为空表示根目录，depth 默认 2）。")
    def kb_tree(path: str = "", depth: int = 2) -> str:
        return _json(store.tree(path, depth=min(int(depth or 2), 4)))

    @mcp.tool(name="kb_glob", description="按通配符列文件，例如 **/*.md 或 *K1*。")
    def kb_glob(pattern: str, limit: int = 100) -> str:
        return _json({"pattern": pattern, "files": store.glob_files(pattern, limit=min(int(limit or 100), 300))})

    @mcp.tool(name="kb_grep", description="在文本文件里做精确/正则检索（对应 grep -n），可返回上下文行。")
    def kb_grep(q: str, regex: bool = False, context: int = 0, limit: int = 20) -> str:
        return _json(store.grep(q, regex=regex, limit=min(int(limit or 20), 100), context=min(int(context or 0), 5)))

    @mcp.tool(name="kb_figure", description="读取图片/图表内容（返回图片给模型）。path 用 kb_read 返回的 images[].path。")
    def kb_figure(path: str):
        target = _resolve_figure(path)
        if target is None:
            return _json({"error": f"图片不存在或不在 content/ 内: {path}"})
        return Image(path=str(target))

    return mcp


def _base_url() -> str:
    cfg = config.load_config()
    # Behind a reverse proxy/subpath, the public URL is not the bind address.
    public_base = str(cfg["server"].get("public_base_url") or "").strip().rstrip("/")
    if public_base:
        return public_base
    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _resolve_figure(path: str):
    candidate = path.replace("\\", "/").strip()
    for prefix in ("content/", "/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    target = util.safe_under(config.CONTENT, candidate)
    if target is not None and target.exists() and target.is_file() and util.is_image_file(target):
        return target
    return None
