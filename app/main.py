"""受限 WebDAV 文本工作区的 HTTP 层。

仅支持已有资源的 GET / PUT / LOCK / UNLOCK；集合只用于锁定。
不支持创建（MKCOL/POST）、移动（MOVE/COPY）、删除、共享锁与账号系统。
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .clock import SystemClock
from .ifheader import IfHeaderError, parse_if_header, submitted_tokens
from .movement import move_tree
from .store import Workspace, norm_path

DAV = "{DAV:}"
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600
ALLOW = "GET, PUT, LOCK, UNLOCK"


def _parse_timeout(value: Optional[str]) -> Optional[int]:
    """解析 Timeout 头，返回秒数；None 表示 Infinite（不过期）。"""
    if value is None:
        return DEFAULT_TIMEOUT
    for part in value.split(","):
        low = part.strip().lower()
        if low.startswith("second-"):
            seconds = int(low[len("second-") :])
            if seconds <= 0:
                raise ValueError("超时必须为正数")
            return min(seconds, MAX_TIMEOUT)
        if low == "infinite":
            return None
    raise ValueError(f"无法识别的 Timeout 头: {value!r}")


def _parse_if_match(value: str) -> str:
    v = value.strip()
    if v == "*" or v.lower().startswith("w/"):
        raise IfHeaderError("If-Match 只接受强 ETag")
    if "," in v or not (v.startswith('"') and v.endswith('"')):
        raise IfHeaderError("If-Match 需要单个强 ETag")
    return v


def _lockdiscovery_xml(token: str, depth_infinity: bool, timeout: Optional[int]) -> str:
    depth = "infinity" if depth_infinity else "0"
    timeout_str = "Infinite" if timeout is None else f"Second-{timeout}"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
        "<D:locktype><D:write/></D:locktype>"
        "<D:lockscope><D:exclusive/></D:lockscope>"
        f"<D:depth>{depth}</D:depth>"
        f"<D:timeout>{timeout_str}</D:timeout>"
        f"<D:locktoken><D:href>{token}</D:href></D:locktoken>"
        "</D:activelock></D:lockdiscovery></D:prop>"
    )


def _error(status: int, message: str, headers: Optional[dict] = None) -> Response:
    return Response(
        content=message + "\n",
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


def _handle_get(ws: Workspace, path: str) -> Response:
    status, content, etag = ws.get_file(path)
    if status == "not_found":
        return _error(404, "资源不存在，且本服务不支持创建")
    if status == "collection":
        return _error(405, "集合只用于锁定，不支持 GET", {"Allow": ALLOW})
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"ETag": etag},
    )


async def _handle_put(ws: Workspace, request: Request, path: str) -> Response:
    raw = await request.body()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _error(400, "正文必须是 UTF-8 文本")

    if_lists = None
    if_header = request.headers.get("if")
    if if_header is not None:
        try:
            if_lists = parse_if_header(if_header)
        except IfHeaderError as exc:
            return _error(400, f"If 头不受支持: {exc}")

    if_match = None
    if_match_raw = request.headers.get("if-match")
    if if_match_raw is not None:
        try:
            if_match = _parse_if_match(if_match_raw)
        except IfHeaderError as exc:
            return _error(400, str(exc))

    outcome = ws.put_file(path, content, if_match, if_lists)
    headers = {"ETag": outcome.etag} if outcome.etag else {}
    if outcome.status == "ok":
        return Response(status_code=204, headers=headers)
    if outcome.status == "not_found":
        return _error(404, "资源不存在，且本服务不支持创建")
    if outcome.status == "collection":
        return _error(405, "集合只用于锁定，不支持 PUT", {"Allow": ALLOW})
    if outcome.status == "locked":
        return _error(423, "资源被锁定：必须在 If 头中提交所有覆盖锁的令牌", headers)
    if outcome.status == "if_match_required":
        return _error(428, "PUT 必须携带强 If-Match 头", headers)
    if outcome.status == "etag_mismatch":
        return _error(412, "If-Match 与当前版本不一致", headers)
    return _error(412, "If 条件不满足", headers)


async def _handle_lock(ws: Workspace, request: Request, path: str) -> Response:
    depth = request.headers.get("depth", "infinity").strip().lower()
    if depth not in ("0", "infinity"):
        return _error(400, "Depth 仅支持 0 或 infinity")
    depth_infinity = depth == "infinity"
    try:
        timeout = _parse_timeout(request.headers.get("timeout"))
    except ValueError as exc:
        return _error(400, str(exc))

    body = (await request.body()).strip()
    if not body:
        return _refresh_lock(ws, request, path, timeout)

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return _error(400, "LOCK 正文不是合法 XML")
    if root.tag != f"{DAV}lockinfo":
        return _error(400, "LOCK 正文必须是 lockinfo")
    scope = root.find(f"{DAV}lockscope")
    ltype = root.find(f"{DAV}locktype")
    if scope is None or ltype is None:
        return _error(400, "lockinfo 缺少 lockscope 或 locktype")
    if scope.find(f"{DAV}shared") is not None:
        return _error(400, "不支持共享锁，仅支持独占写锁")
    if scope.find(f"{DAV}exclusive") is None or ltype.find(f"{DAV}write") is None:
        return _error(400, "仅支持独占写锁")

    outcome = ws.lock(path, depth_infinity, timeout)
    if outcome.status == "not_found":
        return _error(404, "资源不存在，且本服务不支持创建")
    if outcome.status == "conflict":
        return _error(423, "锁冲突：资源已被锁覆盖，或后代存在冲突锁")
    return _lock_response(outcome)


def _refresh_lock(
    ws: Workspace, request: Request, path: str, timeout: Optional[int]
) -> Response:
    if_header = request.headers.get("if")
    if not if_header:
        return _error(400, "刷新锁必须在 If 头中提供现有锁令牌")
    try:
        if_lists = parse_if_header(if_header)
    except IfHeaderError as exc:
        return _error(400, f"If 头不受支持: {exc}")
    outcome = ws.refresh_lock(path, submitted_tokens(if_lists), timeout)
    if outcome.status != "ok":
        # 过期或未知的令牌不能刷新，更不会因此创建新令牌。
        return _error(412, "锁令牌不存在或已过期，无法刷新")
    return _lock_response(outcome)


def _lock_response(outcome) -> Response:
    xml = _lockdiscovery_xml(outcome.token, outcome.depth_infinity, outcome.timeout)
    return Response(
        content=xml,
        status_code=200,
        media_type="application/xml; charset=utf-8",
        headers={"Lock-Token": f"<{outcome.token}>"},
    )


def _handle_unlock(ws: Workspace, request: Request, path: str) -> Response:
    lock_token = request.headers.get("lock-token")
    if not lock_token:
        return _error(400, "缺少 Lock-Token 头")
    token = lock_token.strip()
    if token.startswith("<") and token.endswith(">"):
        token = token[1:-1]
    result = ws.unlock(path, token)
    if result == "ok":
        return Response(status_code=204)
    if result == "path_mismatch":
        return _error(409, "锁令牌属于其他资源")
    return _error(409, "锁令牌不存在或已过期")


def create_app(db_path: Optional[str] = None, clock=None) -> FastAPI:
    ws = Workspace(
        db_path or os.environ.get("DB_PATH", ":memory:"), clock or SystemClock()
    )
    app = FastAPI(
        title="受限 WebDAV 文本工作区", docs_url=None, redoc_url=None, openapi_url=None
    )
    static_file = Path(__file__).parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(static_file)

    @app.post("/api/move", include_in_schema=False)
    async def move(request: Request):
        try:
            result = move_tree(ws, await request.json())
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
        return JSONResponse(result, status_code=200 if result["ok"] else 409)

    @app.get("/api/files", include_in_schema=False)
    async def api_files() -> dict:
        return {"files": ws.list_files()}

    @app.api_route(
        "/dav/{path:path}",
        methods=[
            "GET",
            "PUT",
            "LOCK",
            "UNLOCK",
            "HEAD",
            "OPTIONS",
            "POST",
            "MKCOL",
            "MOVE",
            "COPY",
            "DELETE",
            "PROPFIND",
            "PATCH",
        ],
        include_in_schema=False,
    )
    async def dav(request: Request) -> Response:
        path = norm_path(request.path_params["path"])
        method = request.method
        if method == "GET":
            return _handle_get(ws, path)
        if method == "PUT":
            return await _handle_put(ws, request, path)
        if method == "LOCK":
            return await _handle_lock(ws, request, path)
        if method == "UNLOCK":
            return _handle_unlock(ws, request, path)
        return _error(405, f"方法 {method} 不受支持", {"Allow": ALLOW})

    return app


app = create_app()
