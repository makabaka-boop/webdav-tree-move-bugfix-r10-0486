"""RFC 4918 If 头的受限子集：仅无标签条件列表，条件为正向锁令牌或强 ETag。

    If: (<opaquelocktoken:aaa> ["3"]) (<opaquelocktoken:bbb>)

多个列表之间按 OR 求值，同一列表内的条件按 AND 求值。
Not 条件与带标签列表（tagged list）明确不支持，解析时直接报错。
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\s*(<[^>]*>|\[[^\]]*\]|\(|\)|Not\b)")


class IfHeaderError(ValueError):
    """If 头语法错误，或使用了 Not / 带标签列表 / 弱 ETag 等不支持的特性。"""


def parse_if_header(value: str) -> list[list[tuple[str, str]]]:
    """把 If 头解析为条件列表的列表，条件为 ("token", 令牌) 或 ("etag", ETag)。"""
    tokens: list[str] = []
    pos = 0
    while pos < len(value):
        m = _TOKEN_RE.match(value, pos)
        if not m:
            raise IfHeaderError(f"无法解析的 If 头片段: {value[pos:]!r}")
        tokens.append(m.group(1))
        pos = m.end()
    if not tokens:
        raise IfHeaderError("空的 If 头")
    if tokens[0].startswith("<"):
        raise IfHeaderError("不支持带标签的条件列表（tagged list）")

    lists: list[list[tuple[str, str]]] = []
    i = 0
    while i < len(tokens):
        if tokens[i] != "(":
            raise IfHeaderError("条件列表必须以 ( 开始")
        i += 1
        conditions: list[tuple[str, str]] = []
        while i < len(tokens) and tokens[i] != ")":
            tok = tokens[i]
            if tok == "Not":
                raise IfHeaderError("不支持 Not 条件")
            if tok == "(":
                raise IfHeaderError("条件列表不允许嵌套")
            if tok.startswith("<"):
                conditions.append(("token", tok[1:-1]))
            else:  # [entity-tag]
                etag = tok[1:-1].strip()
                if etag.lower().startswith("w/"):
                    raise IfHeaderError("不支持弱 ETag 比较")
                conditions.append(("etag", etag))
            i += 1
        if i >= len(tokens):
            raise IfHeaderError("条件列表缺少右括号")
        i += 1
        if not conditions:
            raise IfHeaderError("空条件列表")
        lists.append(conditions)
    return lists


def evaluate_if(
    lists: list[list[tuple[str, str]]],
    covering_tokens: set[str],
    current_etag: str,
) -> bool:
    """多列表 OR、列表内 AND。

    令牌条件：该令牌是覆盖此资源的某个有效锁的令牌；
    ETag 条件：与资源当前强 ETag 完全相等。
    """
    for conditions in lists:
        if all(
            (val in covering_tokens) if kind == "token" else (val == current_etag)
            for kind, val in conditions
        ):
            return True
    return False


def submitted_tokens(lists: list[list[tuple[str, str]]]) -> set[str]:
    """If 头中提交的全部锁令牌（用于"必须满足所有覆盖锁"检查）。"""
    return {val for conditions in lists for kind, val in conditions if kind == "token"}
