"""整树迁移：请求级校验 + 存储层单事务裁决。

POST /api/move，JSON 含 source、destination、versions（源子树每个文件路径
对应的强 ETag）和 tokens（所持锁令牌数组）。

- 只把已有文件或完整目录迁入已存在集合下尚不存在的路径，不覆盖目标；
- 目录本身及子目录随文件一起迁移；
- 正文与强 ETag（版本号）不因位置变化而改变；
- 覆盖源或目标的有效锁都必须在 tokens 中满足；源子树内的锁保留令牌、
  深度和到期时刻并随对应资源迁移；
- 过期锁按同一裁决时刻处理（同批共用一个时钟读数）；
- 任一条件不满足，整组保留原树和锁；
- 禁止根目录迁移、迁入自身/后代，以及点段、不完整/多余的版本清单。
"""

from .store import MoveRejected, norm_path


class MoveRequestError(ValueError):
    """请求格式/参数本身不合法（400），区别于裁决失败（4xx 冲突类）。"""


def _clean_path(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MoveRequestError(f"{field} 必须是非空路径字符串")
    normalized = norm_path(value.strip())
    segments = normalized.split("/")[1:]
    if any(seg in (".", "..") or seg == "" for seg in segments):
        raise MoveRequestError(f"{field} 含非法路径段（点段或空段）")
    return normalized


def move_tree(ws, request):
    """返回 {"ok": True, "moved": [...]}；请求级问题抛 MoveRequestError，
    存储层裁决失败抛 MoveRejected，由 HTTP 层映射状态码。"""
    if not isinstance(request, dict):
        raise MoveRequestError("请求体必须是 JSON 对象")

    source = _clean_path(request.get("source"), "source")
    destination = _clean_path(request.get("destination"), "destination")

    if source == "/" or destination == "/":
        raise MoveRequestError("禁止迁移根目录")
    if source == destination:
        raise MoveRequestError("源与目标相同")
    if destination.startswith(source + "/"):
        raise MoveRequestError("目标不能位于源自身或源的后代之下")

    raw_versions = request.get("versions")
    if not isinstance(raw_versions, dict):
        raise MoveRequestError("versions 必须是 文件路径 -> 强 ETag 的对象")
    versions: dict[str, str] = {}
    for key, value in raw_versions.items():
        path = _clean_path(key, "versions 的键")
        if not isinstance(value, str):
            raise MoveRequestError(f"{path} 的版本必须是强 ETag 字符串")
        etag = value.strip()
        if etag.lower().startswith("w/") or not (
            etag.startswith('"') and etag.endswith('"')
        ):
            raise MoveRequestError(f"{path} 的版本必须是单个强 ETag")
        versions[path] = etag

    raw_tokens = request.get("tokens", [])
    if not isinstance(raw_tokens, list) or not all(
        isinstance(t, str) for t in raw_tokens
    ):
        raise MoveRequestError("tokens 必须是锁令牌字符串数组")
    tokens = {t.strip().strip("<>") for t in raw_tokens if t.strip()}

    entries = ws.move_tree(source, destination, versions, tokens)
    return {
        "ok": True,
        "moved": [
            {
                "from": e.src,
                "to": e.dst,
                "is_collection": e.is_collection,
                **({"etag": e.etag} if e.etag is not None else {}),
            }
            for e in entries
        ],
    }
