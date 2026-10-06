"""POST /api/move 的请求校验：把已有文件或完整目录迁入已存在集合下的空路径。

路径形状（根目录、自身/后代、点段）与字段类型在此校验；
版本清单、有效锁与路径重写的原子裁决由 Workspace.move 在单笔事务内完成。
"""

from .store import is_ancestor, norm_path


def _clean_path(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    if any(seg in (".", "..") for seg in value.split("/")):
        raise ValueError(f"{field} 不允许包含点段: {value!r}")
    return norm_path(value)


def move_tree(ws, request) -> dict:
    if not isinstance(request, dict):
        raise ValueError("请求体必须是 JSON 对象")
    source = _clean_path(request["source"], "source")
    destination = _clean_path(request["destination"], "destination")
    versions = request["versions"]
    tokens = request["tokens"]
    if not isinstance(versions, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in versions.items()
    ):
        raise ValueError("versions 必须是 {文件路径: 强ETag} 的对象")
    if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
        raise ValueError("tokens 必须是锁令牌数组")
    if source == "/":
        raise ValueError("禁止迁移根目录")
    if destination == source or is_ancestor(source, destination):
        raise ValueError("目标路径不能是源自身或源的后代")

    outcome = ws.move(
        source,
        destination,
        {norm_path(k): v for k, v in versions.items()},
        set(tokens),
    )
    if outcome.status == "ok":
        return {"ok": True, "moved": outcome.moved}
    if outcome.status == "incomplete_versions":
        raise ValueError(f"版本清单不完整，缺少文件: {outcome.path}")
    reasons = {
        "source_not_found": "源不存在",
        "destination_exists": "目标已存在，不允许覆盖",
        "parent_not_collection": "目标父路径不是已存在的集合",
        "version_conflict": "文件版本已过期",
        "locked": "源或目标被未提交令牌的有效锁保护",
    }
    reason = reasons.get(outcome.status, outcome.status)
    if outcome.path:
        reason = f"{reason}: {outcome.path}"
    return {"ok": False, "reason": reason}
