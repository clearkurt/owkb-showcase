"""HTTP service: REST API + MCP streamable endpoint behind a bearer-token gate."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.routing import Mount

from . import config, index, store, tokens, uploads, util
from .mcp_tools import build_mcp

PUBLIC_PATHS = {"/", "/health", "/openapi.json", "/docs", "/redoc", "/favicon.ico", "/api/enroll"}

_access_logger = logging.getLogger("owkb.access")


class EnrollRequest(BaseModel):
    initials: str
    owner: str | None = None


def _configure_logging() -> None:
    if _access_logger.handlers:
        return
    config.LOGS.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(config.LOGS / "access.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _access_logger.addHandler(handler)
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False


def _load_token_hashes() -> list[tuple[str, str]]:
    """Compatibility wrapper; token source is app.tokens."""
    return tokens.token_hashes()


def _verify_token(authorization: str) -> str | None:
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    return tokens.verify_token(token)


def _lookup_token(authorization: str) -> dict[str, Any] | None:
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    return tokens.lookup_token(token)


class TokenAuthMiddleware:
    """Pure ASGI middleware: bearer token required for /api and /mcp."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        token_entry = _lookup_token(authorization)
        if token_entry is None:
            response = JSONResponse({"error": "unauthorized: missing or invalid bearer token"}, status_code=401)
            return await response(scope, receive, send)

        token_name = str(token_entry.get("name", "unknown"))
        device_id = headers.get(b"x-owkb-device", b"").decode("latin-1").strip()
        device_name = headers.get(b"x-owkb-device-name", b"").decode("latin-1").strip()[:128]
        bound_device = str(token_entry.get("device_id") or "")

        if bound_device:
            if not device_id or not hmac.compare_digest(bound_device, device_id):
                _access_logger.info("DEVICE_MISMATCH token=%s device=%s status=403", token_name, device_id[:12])
                response = JSONResponse({"error": "device mismatch: token is bound to another computer"}, status_code=403)
                return await response(scope, receive, send)
        elif device_id:
            try:
                tokens.bind_token_device(token_name, device_id, device_name)
            except tokens.TokenError:
                response = JSONResponse({"error": "invalid device id"}, status_code=403)
                return await response(scope, receive, send)
            _access_logger.info("DEVICE_BIND token=%s device=%s name=%s", token_name, device_id[:12], device_name)

        scope["owkb_token"] = token_name
        scope["owkb_device"] = device_id
        scope["owkb_device_name"] = device_name

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                _access_logger.info(
                    "%s %s token=%s device=%s status=%s",
                    scope.get("method"),
                    path,
                    token_name,
                    device_id[:12] or "unbound",
                    message["status"],
                )
            await send(message)

        return await self.app(scope, receive, send_wrapper)


def _api() -> FastAPI:
    api = FastAPI(
        title="OWKB Knowledge Base API",
        version="0.1.0",
        description="只读知识库：检索、读取、目录浏览、grep、附件下载。",
    )

    @api.get("/")
    def root() -> dict[str, Any]:
        return {
            "service": "owkb",
            "readonly": True,
            "docs": "/docs",
            "health": "/health",
            "token": "Authorization: Bearer <OWKB_TOKEN>",
        }

    @api.get("/health")
    def health() -> dict[str, Any]:
        result = index.health()
        result["catalog_docs"] = len(store.load_catalog().get("docs", {}))
        return result

    @api.get("/api/whoami")
    def whoami() -> dict[str, Any]:
        return {"ok": True}

    @api.post("/api/uploads")
    async def create_upload(
        request: Request,
        kind: str = Query("project"),
        filename: str = Query("payload.zip"),
        name: str = Query(""),
        mcu: str = Query(""),
        note: str = Query(""),
    ) -> JSONResponse:
        """Accept a raw upload into the quarantine area; review happens separately."""
        try:
            manifest = await uploads.save_stream(
                request.stream(),
                kind=kind,
                filename=filename,
                name=name,
                mcu=mcu,
                note=note,
                uploader=str(request.scope.get("owkb_token", "")),
                device_id=str(request.scope.get("owkb_device", "")),
                device_name=str(request.scope.get("owkb_device_name", "")),
            )
        except uploads.UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except uploads.UploadQuotaExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except uploads.UploadRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except uploads.UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "id": manifest["id"],
                "status": manifest["status"],
                "size": manifest["size"],
                "sha256": manifest["sha256"],
            },
            status_code=202,
        )

    @api.post("/api/enroll")
    def enroll(
        payload: EnrollRequest,
        request: Request,
        x_owkb_enroll: str | None = Header(default=None, alias="X-OWKB-Enroll"),
    ) -> dict[str, Any]:
        """Self-service enrollment: exchange an enrollment code for one personal token."""
        ip = request.client.host if request.client else ""
        if not tokens.verify_enroll_code(x_owkb_enroll or ""):
            _access_logger.info("ENROLL ip=%s status=401", ip)
            raise HTTPException(status_code=401, detail="invalid enrollment code")
        try:
            name = tokens.normalize_name(payload.initials)
        except tokens.TokenError as exc:
            _access_logger.info("ENROLL ip=%s status=400 reason=bad_name", ip)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        owner = (payload.owner or ip).strip()[:128]
        try:
            _entry, token = tokens.create_token(
                name,
                owner=owner,
                note="self-enroll",
                rotate_if_owner=True,
            )
        except tokens.TokenExists as exc:
            _access_logger.info("ENROLL name=%s ip=%s status=409", name, ip)
            raise HTTPException(
                status_code=409,
                detail=f"{exc}; 如果这是你本人重装，请让管理员删除旧 token 后重试",
            ) from exc
        _access_logger.info("ENROLL name=%s owner=%s ip=%s status=200", name, owner, ip)
        return {"ok": True, "name": name, "token": token}


    @api.get("/api/search")
    def search(
        q: str = Query(..., min_length=1),
        limit: int = Query(5, ge=1, le=20),
        collection: str | None = None,
        tag: str | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, Any]:
        return index.search(q, limit=limit, collection=collection, tag=tag, path_prefix=path_prefix)

    @api.get("/api/read")
    def read(
        id: str | None = None,
        path: str | None = None,
        page: int | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        ref = id or path
        if not ref:
            raise HTTPException(status_code=400, detail="id or path is required")
        doc = store.resolve(ref)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"document not found: {ref}")
        return store.read(doc, page=page, start=start, end=end)

    @api.get("/api/tree")
    def tree(path: str = "", depth: int = Query(2, ge=1, le=4)) -> dict[str, Any]:
        return store.tree(path, depth=depth)

    @api.get("/api/ls")
    def ls(path: str = "") -> dict[str, Any]:
        return store.ls(path)

    @api.get("/api/glob")
    def glob(pattern: str, limit: int = Query(100, ge=1, le=300)) -> dict[str, Any]:
        return {"pattern": pattern, "files": store.glob_files(pattern, limit=limit)}

    @api.get("/api/grep")
    def grep(
        q: str = Query(..., min_length=1),
        regex: bool = False,
        context: int = Query(0, ge=0, le=5),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        return store.grep(q, regex=regex, limit=limit, context=context)

    @api.get("/api/stat")
    def stat(path: str) -> dict[str, Any]:
        return store.stat_path(path)

    @api.get("/api/tags")
    def tags() -> dict[str, Any]:
        return {"tags": index.all_tags()}

    @api.get("/api/download")
    def download(path: str) -> FileResponse:
        candidate = path.replace("\\", "/")
        if candidate.startswith("content/") or candidate.startswith("library/"):
            target = util.safe_under(config.ROOT, candidate)
        else:
            target = util.safe_under(config.CONTENT, candidate)
        if target is None or not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {path}")
        return FileResponse(target, filename=target.name)

    return api


def create_app(host: str | None = None, port: int | None = None):
    _configure_logging()
    index.ensure_index()
    from . import autoindex

    autoindex.start()
    cfg = config.load_config()
    host = host or str(cfg["server"]["host"])
    port = port or int(cfg["server"]["port"])

    mcp = build_mcp()
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_asgi = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        host=host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp_asgi.router.routes.append(Mount("/", app=_api()))
    mcp_asgi.add_middleware(TokenAuthMiddleware)
    return mcp_asgi


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    cfg = config.load_config()
    host = host or str(cfg["server"]["host"])
    port = port or int(cfg["server"]["port"])
    app = create_app(host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
