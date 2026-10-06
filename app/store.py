"""SQLite 存储层：固定目录树、正文、ETag 与锁。

所有涉及锁状态、到期判断、条件验证与正文更新的裁决都在单笔
BEGIN IMMEDIATE 事务内完成；任何失败路径都会回滚，
失败请求不会改正文或版本号。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    path          TEXT PRIMARY KEY,
    is_collection INTEGER NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    etag          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS locks (
    token          TEXT PRIMARY KEY,
    path           TEXT NOT NULL,
    depth_infinity INTEGER NOT NULL,
    expires_at     REAL,          -- NULL 表示永不过期
    created_at     REAL NOT NULL
);
"""

# 固定目录树：服务启动时播种，之后只改正文与版本，不增删节点。
SEED = [
    ("/", 1, "", 1),
    ("/docs", 1, "", 1),
    ("/docs/a.txt", 0, "alpha\n", 1),
    ("/docs/b.txt", 0, "beta\n", 1),
    ("/notes", 1, "", 1),
    ("/notes/todo.txt", 0, "todo\n", 1),
    ("/readme.txt", 0, "hello\n", 1),
]


def norm_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def is_ancestor(ancestor: str, path: str) -> bool:
    """ancestor 是否为 path 的严格祖先（不含自身）。"""
    if ancestor == "/":
        return path != "/"
    return path.startswith(ancestor + "/")


def parent_path(path: str) -> str:
    """父路径；根目录的父路径仍是根。"""
    if path == "/":
        return "/"
    return path.rsplit("/", 1)[0] or "/"


def lock_covers(lock_path: str, depth_infinity: bool, path: str) -> bool:
    """锁是否覆盖 path：自身（任意深度），或 Depth infinity 锁的全部后代。"""
    if lock_path == path:
        return True
    return bool(depth_infinity) and is_ancestor(lock_path, path)


def etag_str(version: int) -> str:
    return f'"{version}"'


@dataclass
class PutOutcome:
    status: str  # ok | not_found | collection | locked | if_match_required | etag_mismatch | if_failed
    etag: Optional[str] = None  # 当前（或新的）ETag


@dataclass
class LockOutcome:
    status: str  # ok | not_found | conflict
    token: Optional[str] = None
    depth_infinity: bool = False
    timeout: Optional[int] = None  # None 表示 Infinite


@dataclass
class MoveOutcome:
    # ok | source_not_found | destination_exists | parent_not_collection
    # | incomplete_versions | version_conflict | locked
    status: str
    moved: Optional[list] = None  # 成功时迁移条目的 from/to（文件附未变的 ETag）
    path: Optional[str] = None  # 失败时冲突涉及的路径（诊断信息）


class Workspace:
    def __init__(self, db_path: str, clock):
        self.clock = clock
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.isolation_level = None  # 显式管理事务
        self._conn.row_factory = sqlite3.Row
        self._mu = threading.RLock()
        # 自动提交模式下播种固定目录树（executescript 会隐式提交，不能放在 txn 里）
        self._conn.executescript(SCHEMA)
        self._conn.executemany(
            "INSERT OR IGNORE INTO resources(path, is_collection, content, etag)"
            " VALUES (?, ?, ?, ?)",
            SEED,
        )

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """单笔可写事务：BEGIN IMMEDIATE 保证裁决期间无其他写者。"""
        with self._mu:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---- 内部：必须在事务内调用 ----

    def _purge_expired(self, c: sqlite3.Connection) -> None:
        """到期判断与锁状态在同一事务内：过期锁立即视为不存在。"""
        c.execute(
            "DELETE FROM locks WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (self.clock.now(),),
        )

    def _resource(self, c: sqlite3.Connection, path: str):
        return c.execute("SELECT * FROM resources WHERE path = ?", (path,)).fetchone()

    def _covering_locks(self, c: sqlite3.Connection, path: str):
        rows = c.execute("SELECT * FROM locks").fetchall()
        return [r for r in rows if lock_covers(r["path"], r["depth_infinity"], path)]

    # ---- 查询 ----

    def list_files(self) -> list[str]:
        with self.txn() as c:
            rows = c.execute(
                "SELECT path FROM resources WHERE is_collection = 0 ORDER BY path"
            ).fetchall()
            return [r["path"] for r in rows]

    def get_file(self, path: str):
        """返回 (status, content, etag)，status ∈ ok | not_found | collection。"""
        with self.txn() as c:
            row = self._resource(c, path)
            if row is None:
                return ("not_found", None, None)
            if row["is_collection"]:
                return ("collection", None, None)
            return ("ok", row["content"], etag_str(row["etag"]))

    # ---- PUT ----

    def put_file(
        self, path: str, content: str, if_match: Optional[str], if_lists
    ) -> PutOutcome:
        """在同一事务内裁决覆盖锁、强 If-Match、If 条件，然后更新正文与版本。"""
        from .ifheader import evaluate_if, submitted_tokens

        with self.txn() as c:
            self._purge_expired(c)
            row = self._resource(c, path)
            if row is None:
                return PutOutcome("not_found")
            if row["is_collection"]:
                return PutOutcome("collection")
            current = etag_str(row["etag"])

            covering = self._covering_locks(c, path)
            submitted = submitted_tokens(if_lists) if if_lists else set()
            # PUT 必须满足所有覆盖锁：每个覆盖锁的令牌都要在 If 头中提交。
            if any(l["token"] not in submitted for l in covering):
                return PutOutcome("locked", current)

            if if_match is None:
                return PutOutcome("if_match_required", current)
            if if_match != current:
                return PutOutcome("etag_mismatch", current)

            if if_lists is not None:
                covering_tokens = {l["token"] for l in covering}
                if not evaluate_if(if_lists, covering_tokens, current):
                    return PutOutcome("if_failed", current)

            new_version = row["etag"] + 1
            c.execute(
                "UPDATE resources SET content = ?, etag = ? WHERE path = ?",
                (content, new_version, path),
            )
            return PutOutcome("ok", etag_str(new_version))

    # ---- LOCK ----

    def lock(
        self, path: str, depth_infinity: bool, timeout: Optional[int]
    ) -> LockOutcome:
        """新建独占写锁。timeout 为秒，None 表示不过期。"""
        with self.txn() as c:
            self._purge_expired(c)
            if self._resource(c, path) is None:
                return LockOutcome("not_found")
            # 覆盖此资源的锁（自身锁或祖先的 Depth infinity 锁）即冲突。
            if self._covering_locks(c, path):
                return LockOutcome("conflict")
            # Depth infinity：后代已存在的任何锁都是冲突锁。
            if depth_infinity:
                for r in c.execute("SELECT path FROM locks").fetchall():
                    if is_ancestor(path, r["path"]):
                        return LockOutcome("conflict")
            token = f"opaquelocktoken:{uuid.uuid4()}"
            now = self.clock.now()
            expires = None if timeout is None else now + timeout
            c.execute(
                "INSERT INTO locks(token, path, depth_infinity, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (token, path, int(depth_infinity), expires, now),
            )
            return LockOutcome("ok", token, depth_infinity, timeout)

    def refresh_lock(
        self, path: str, tokens: set[str], timeout: Optional[int]
    ) -> LockOutcome:
        """刷新已有锁：只延长有效期，绝不创建新令牌。"""
        with self.txn() as c:
            self._purge_expired(c)
            for token in tokens:
                row = c.execute(
                    "SELECT * FROM locks WHERE token = ?", (token,)
                ).fetchone()
                if row is not None and row["path"] == path:
                    expires = None if timeout is None else self.clock.now() + timeout
                    c.execute(
                        "UPDATE locks SET expires_at = ? WHERE token = ?",
                        (expires, token),
                    )
                    return LockOutcome(
                        "ok", token, bool(row["depth_infinity"]), timeout
                    )
            return LockOutcome("not_found")

    # ---- UNLOCK ----

    def unlock(self, path: str, token: str) -> str:
        """返回 ok | not_found | path_mismatch。

        过期锁已被清除，过期令牌查不到任何锁，自然无法解开后来建立的新锁。
        """
        with self.txn() as c:
            self._purge_expired(c)
            row = c.execute("SELECT * FROM locks WHERE token = ?", (token,)).fetchone()
            if row is None:
                return "not_found"
            if row["path"] != path:
                return "path_mismatch"
            c.execute("DELETE FROM locks WHERE token = ?", (token,))
            return "ok"

    # ---- MOVE（整棵子树迁移） ----

    def move(
        self,
        source: str,
        destination: str,
        versions: dict,
        tokens: set,
    ) -> MoveOutcome:
        """把 source（已有文件或完整目录）迁入已存在集合下的空路径 destination。

        版本清单、覆盖源/目标的有效锁与路径重写在同一事务内裁决：
        整批锁按同一个到期时刻判断，任一条件不满足就整体回滚——
        目录、文件、正文、版本与锁（令牌、深度、剩余有效期）全部保持原状。
        成功时正文与 ETag 不因位置变化而改变，源内锁随对应资源迁移。
        """
        with self.txn() as c:
            self._purge_expired(c)  # 同一批操作共用这一个裁决时刻
            if self._resource(c, source) is None:
                return MoveOutcome("source_not_found", path=source)
            if self._resource(c, destination) is not None:
                return MoveOutcome("destination_exists", path=destination)
            dest_parent = parent_path(destination)
            parent = self._resource(c, dest_parent)
            if parent is None or not parent["is_collection"]:
                return MoveOutcome("parent_not_collection", path=dest_parent)

            rows = [
                r
                for r in c.execute("SELECT * FROM resources ORDER BY path").fetchall()
                if r["path"] == source or is_ancestor(source, r["path"])
            ]
            # 版本清单必须覆盖源子树的每个文件，且与当前强 ETag 一致。
            for r in rows:
                if r["is_collection"]:
                    continue
                submitted = versions.get(r["path"])
                if submitted is None:
                    return MoveOutcome("incomplete_versions", path=r["path"])
                if submitted != etag_str(r["etag"]):
                    return MoveOutcome("version_conflict", path=r["path"])

            # 覆盖源子树任一资源、目标路径或目标父目录（成员变更）的有效锁，
            # 其令牌都必须出现在 tokens 中。
            locks = c.execute("SELECT * FROM locks").fetchall()
            covered = [r["path"] for r in rows] + [destination]
            for l in locks:
                if l["token"] in tokens:
                    continue
                if l["path"] == dest_parent or any(
                    lock_covers(l["path"], l["depth_infinity"], p) for p in covered
                ):
                    return MoveOutcome("locked", path=l["path"])

            # 全部条件满足：只重写路径，正文与版本原样保留。
            moved = []
            for r in rows:
                new_path = destination + r["path"][len(source):]
                c.execute(
                    "UPDATE resources SET path = ? WHERE path = ?",
                    (new_path, r["path"]),
                )
                entry = {"from": r["path"], "to": new_path}
                if not r["is_collection"]:
                    entry["etag"] = etag_str(r["etag"])
                moved.append(entry)
            # 源内锁随对应资源迁移：令牌、深度与到期时刻原样保留。
            for l in locks:
                if l["path"] == source or is_ancestor(source, l["path"]):
                    new_path = destination + l["path"][len(source):]
                    c.execute(
                        "UPDATE locks SET path = ? WHERE token = ?",
                        (new_path, l["token"]),
                    )
            return MoveOutcome("ok", moved=moved)
