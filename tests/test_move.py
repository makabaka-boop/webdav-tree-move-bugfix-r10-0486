"""端到端测试：POST /api/move 整树迁移。

全部请求经 HTTP 层发出（FastAPI TestClient），覆盖：
- 成功时整树（含空目录、嵌套子目录）迁移，正文、版本（强 ETag）不变；
- 锁令牌随资源改路径，深度与剩余有效期（绝对到期时刻）保留；
- 祖先锁留在原处继续覆盖新路径，旧路径上的令牌行为正确失效；
- 版本过期、源/目标父/后代有效锁未满足 → 412/423，且整组回滚
  （目录、文件、连同过期锁清理在内的锁状态全部保持原状）；
- 同批有效锁按同一裁决时刻判断。
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.main import create_app

LOCK_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:lockinfo xmlns:D="DAV:">'
    "<D:lockscope><D:exclusive/></D:lockscope>"
    "<D:locktype><D:write/></D:locktype>"
    "</D:lockinfo>"
)


@pytest.fixture()
def env():
    clock = FakeClock()
    client = TestClient(create_app(db_path=":memory:", clock=clock))
    return clock, client


@pytest.fixture()
def env_file(tmp_path):
    clock = FakeClock()
    db_path = str(tmp_path / "ws.db")
    client = TestClient(create_app(db_path=db_path, clock=clock))
    return clock, client, db_path


def do_lock(client, path, depth="0", timeout="Second-300", body=LOCK_XML):
    return client.request(
        "LOCK",
        "/dav" + path,
        content=body,
        headers={"Depth": depth, "Timeout": timeout, "Content-Type": "application/xml"},
    )


def lock_token(response):
    assert response.status_code == 200, response.text
    return response.headers["lock-token"].strip("<>")


def do_unlock(client, path, token):
    return client.request(
        "UNLOCK", "/dav" + path, headers={"Lock-Token": f"<{token}>"}
    )


def do_put(client, path, content, etag=None, if_header=None):
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if etag is not None:
        headers["If-Match"] = etag
    if if_header is not None:
        headers["If"] = if_header
    return client.put("/dav" + path, content=content, headers=headers)


def get(client, path):
    return client.get("/dav" + path)


def move(client, source, destination, versions=None, tokens=None):
    return client.post(
        "/api/move",
        json={
            "source": source,
            "destination": destination,
            "versions": versions or {},
            "tokens": tokens or [],
        },
    )


def seed_versions(client, *files):
    return {f: get(client, f).headers["etag"] for f in files}


DOCS_FILES = ("/docs/a.txt", "/docs/b.txt")


# ---------- 成功：整树、正文与版本 ----------


def test_move_whole_tree_preserves_content_and_versions(env):
    _, c = env
    before = {f: (get(c, f).text, get(c, f).headers["etag"]) for f in DOCS_FILES}

    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 200, r.text
    entries = {e["from"]: e for e in r.json()["moved"]}
    assert set(entries) == {"/docs", "/docs/a.txt", "/docs/b.txt"}
    assert entries["/docs"]["is_collection"] is True
    assert entries["/docs/a.txt"]["etag"] == before["/docs/a.txt"][1]
    assert entries["/docs/b.txt"]["etag"] == before["/docs/b.txt"][1]

    # 文件内容与强 ETag 在新路径上保持不变；旧路径全部消失（无空目录残留）。
    for f in DOCS_FILES:
        new = "/notes" + f
        g = get(c, new)
        assert g.status_code == 200
        assert (g.text, g.headers["etag"]) == before[f]
        assert get(c, f).status_code == 404
    assert do_lock(c, "/docs", depth="0").status_code == 404

    # ETag 仍是同一个版本号：用迁移前的版本做 If-Match PUT 可直接串行写入。
    etag = before["/docs/a.txt"][1]
    r = do_put(c, "/notes/docs/a.txt", "新位置的更新", etag=etag)
    assert r.status_code == 204
    g = get(c, "/notes/docs/a.txt")
    assert g.text == "新位置的更新" and g.headers["etag"] != etag

    files = c.get("/api/files").json()["files"]
    assert "/docs/a.txt" not in files and "/notes/docs/a.txt" in files


def test_move_includes_empty_and_nested_collections(env_file):
    _, c, db_path = env_file
    # 固定树之外补入空集合与嵌套子目录（直接写库）。
    with sqlite3.connect(db_path) as raw:
        raw.executemany(
            "INSERT INTO resources(path, is_collection, content, etag)"
            " VALUES (?, ?, ?, ?)",
            [
                ("/docs/empty", 1, "", 1),
                ("/docs/sub", 1, "", 1),
                ("/docs/sub/c.txt", 0, "cee\n", 1),
            ],
        )

    versions = seed_versions(c, "/docs/a.txt", "/docs/b.txt", "/docs/sub/c.txt")
    r = move(c, "/docs", "/notes/docs", versions)
    assert r.status_code == 200, r.text
    moved = {e["from"]: e for e in r.json()["moved"]}
    assert moved["/docs/empty"]["is_collection"] is True
    assert moved["/docs/sub"]["is_collection"] is True
    assert moved["/docs/sub/c.txt"]["etag"] == '"1"'

    # 空集合与嵌套集合都在新位置可锁（说明节点真的迁过去了）。
    assert do_lock(c, "/notes/docs/empty", depth="0").status_code == 200
    assert do_lock(c, "/notes/docs/sub", depth="0").status_code == 200
    g = get(c, "/notes/docs/sub/c.txt")
    assert g.text == "cee\n" and g.headers["etag"] == '"1"'

    assert do_lock(c, "/docs/sub", depth="0").status_code == 404
    assert do_lock(c, "/docs/empty", depth="0").status_code == 404


# ---------- 成功：锁随资源迁移 ----------


def test_moved_file_lock_token_works_only_at_new_path(env):
    _, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token])
    assert r.status_code == 200, r.text

    # 新路径：无令牌被锁挡住，令牌可用；旧路径：404，旧令牌报路径不符。
    etag = get(c, "/notes/docs/a.txt").headers["etag"]
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 423
    assert do_unlock(c, "/docs/a.txt", token).status_code == 409
    assert do_put(
        c, "/notes/docs/a.txt", "持令牌写入", etag=etag, if_header=f"(<{token}>)"
    ).status_code == 204
    assert do_unlock(c, "/notes/docs/a.txt", token).status_code == 204
    # 解锁后新路径可重新加锁。
    lock_token(do_lock(c, "/notes/docs/a.txt", depth="0"))


def test_moved_lock_keeps_absolute_expiry(env):
    """锁的到期时刻是绝对时间：迁移不重置剩余有效期。"""
    clock, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-30"))
    clock.advance(20)  # 剩 10s 时迁移
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token])
    assert r.status_code == 200, r.text

    clock.advance(5)  # 距到期还有 5s，新路径仍被锁
    assert do_lock(c, "/notes/docs/a.txt", depth="0").status_code == 423
    clock.advance(6)  # 超过原始到期时刻；若迁移时重置过，此刻仍会 423
    assert do_lock(c, "/notes/docs/a.txt", depth="0").status_code == 200


def test_moved_depth_infinity_lock_covers_new_descendants(env):
    _, c = env
    token = lock_token(do_lock(c, "/docs", depth="infinity"))
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token])
    assert r.status_code == 200, r.text

    # 深度锁随目录迁到新路径，继续保护后代。
    etag = get(c, "/notes/docs/b.txt").headers["etag"]
    assert do_put(c, "/notes/docs/b.txt", "x", etag=etag).status_code == 423
    assert do_put(
        c, "/notes/docs/b.txt", "持令牌", etag=etag, if_header=f"(<{token}>)"
    ).status_code == 204

    # 新路径上刷新返回同一令牌，不创建新锁。
    r = c.request(
        "LOCK",
        "/dav/notes/docs",
        headers={"If": f"(<{token}>)", "Timeout": "Second-500"},
    )
    assert r.status_code == 200
    assert r.headers["lock-token"].strip("<>") == token
    assert do_unlock(c, "/notes/docs", token).status_code == 204


def test_ancestor_lock_stays_and_protects_destination(env):
    """源的祖先（目标的祖先）锁不在子树内，路径不变并继续覆盖迁移结果。"""
    _, c = env
    token = lock_token(do_lock(c, "/notes", depth="infinity"))

    # 目标 /notes/docs 受 /notes 深度锁保护：不提交令牌 → 423 且原树不动。
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 423
    assert get(c, "/docs/a.txt").status_code == 200

    # 提交祖先锁令牌后成功；令牌仍在 /notes 上，继续保护迁来的后代。
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token])
    assert r.status_code == 200, r.text
    etag = get(c, "/notes/docs/a.txt").headers["etag"]
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 423
    assert do_unlock(c, "/notes/docs/a.txt", token).status_code == 409  # 锁属于 /notes
    assert do_unlock(c, "/notes", token).status_code == 204


def test_root_lock_covers_move_and_result(env):
    _, c = env
    token = lock_token(do_lock(c, "/", depth="infinity"))
    assert move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES)).status_code == 423
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token])
    assert r.status_code == 200, r.text
    assert do_lock(c, "/notes/docs", depth="0").status_code == 423  # 根锁仍覆盖
    assert do_unlock(c, "/", token).status_code == 204


# ---------- 失败：版本、锁与整体回滚 ----------


def test_stale_version_aborts_and_keeps_tree_and_locks(env_file):
    clock, c, db_path = env_file
    lock_token(do_lock(c, "/readme.txt", depth="0", timeout="Second-60"))
    # 子树外的过期锁：失败回滚必须连过期清理一起撤回，行仍在。
    stale = lock_token(do_lock(c, "/notes/todo.txt", depth="0", timeout="Second-10"))
    clock.advance(11)

    versions = seed_versions(c, *DOCS_FILES)
    versions["/docs/b.txt"] = '"999"'  # 批次中某个文件版本已过期
    r = move(c, "/docs", "/notes/docs", versions)
    assert r.status_code == 412
    assert r.json()["reason"] == "etag_mismatch"

    # 整棵目录保持原状：文件、版本、空目录、目标路径都没变。
    for f in DOCS_FILES:
        assert get(c, f).status_code == 200
    assert get(c, "/notes/docs/a.txt").status_code == 404
    # 无关锁（有效与过期）都未被失败请求改动——必须在任何会触发到期清理的
    # 请求之前直接查库验证：失败事务连过期锁清理都一并回滚。
    with sqlite3.connect(db_path) as raw:
        rows = raw.execute("SELECT token, path FROM locks").fetchall()
    assert (stale, "/notes/todo.txt") in rows
    etag = get(c, "/readme.txt").headers["etag"]
    assert do_put(c, "/readme.txt", "x", etag=etag).status_code == 423


def test_missing_descendant_lock_aborts_without_partial_move(env):
    """批次中间文件受有效锁保护而未提交令牌：失败响应前不得搬走任何文件。"""
    _, c = env
    token_a = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    lock_token(do_lock(c, "/docs/b.txt", depth="0"))  # 未提交的令牌

    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token_a])
    assert r.status_code == 423

    for f in DOCS_FILES:
        assert get(c, f).status_code == 200
        assert get(c, "/notes" + f).status_code == 404
    # 两个锁都原样保留在旧路径上。
    assert do_lock(c, "/docs", depth="infinity").status_code == 423


def test_locked_source_or_destination_parent_aborts(env):
    _, c = env
    # 源上的深度锁
    token = lock_token(do_lock(c, "/docs", depth="infinity"))
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 423
    assert do_unlock(c, "/docs", token).status_code == 204

    # 目标父集合上的深度锁
    token_notes = lock_token(do_lock(c, "/notes", depth="infinity"))
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 423
    assert get(c, "/docs/a.txt").status_code == 200  # 未发生部分迁移
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES), [token_notes])
    assert r.status_code == 200


# ---------- 同一裁决时刻 ----------


def test_expired_locks_judged_at_single_instant_and_purged(env):
    """到期恰好发生：整批按一个时间点判断；过期锁不再保护资源，成功后被清除。"""
    clock, c = env
    lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-10"))
    clock.advance(10) # expires_at <= now，此刻已过期

    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 200, r.text
    # 过期锁没有随迁：新路径可以直接加锁。
    assert do_lock(c, "/notes/docs/a.txt", depth="0").status_code == 200


def test_lock_valid_one_second_before_expiry_blocks_move(env):
    clock, c = env
    lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-10"))
    clock.advance(9)  # 同一时刻仍有效 → 必须提交令牌
    r = move(c, "/docs", "/notes/docs", seed_versions(c, *DOCS_FILES))
    assert r.status_code == 423


# ---------- 请求与结构约束 ----------


@pytest.mark.parametrize(
    "source,destination",
    [
        ("/", "/notes/docs"),
        ("/docs", "/"),
        ("/docs", "/docs"),
        ("/docs", "/docs/child"),
        ("/docs", "/docs/../notes/docs"),
        ("/docs", "/notes//docs"),
        ("/docs", "/notes/./docs"),
    ],
)
def test_rejected_paths(env, source, destination):
    _, c = env
    r = move(c, source, destination, seed_versions(c, *DOCS_FILES))
    assert r.status_code == 400, (source, destination, r.text)


def test_structural_conflicts(env):
    _, c = env
    versions = seed_versions(c, *DOCS_FILES)
    assert move(c, "/missing", "/notes/missing", versions).status_code == 409
    assert move(c, "/docs", "/readme.txt", versions).status_code == 409  # 目标已存在
    assert move(c, "/readme.txt", "/nope/readme.txt", {"/readme.txt": '"1"'}).status_code == 409


def test_version_manifest_must_match_exactly(env):
    _, c = env
    versions = seed_versions(c, *DOCS_FILES)
    missing = dict(versions)
    del missing["/docs/b.txt"]
    assert move(c, "/docs", "/notes/docs", missing).status_code == 400

    extra = dict(versions)
    extra["/readme.txt"] = '"1"'
    assert move(c, "/docs", "/notes/docs", extra).status_code == 400

    weak = dict(versions)
    weak["/docs/a.txt"] = 'W/"1"'
    assert move(c, "/docs", "/notes/docs", weak).status_code == 400


def test_malformed_requests(env):
    _, c = env
    assert c.post("/api/move", content="{not json", headers={}).status_code == 400
    assert c.post("/api/move", json={"source": "/docs"}).status_code == 400
    assert (
        c.post(
            "/api/move",
            json={
                "source": "/docs",
                "destination": "/notes/docs",
                "versions": [],
                "tokens": [],
            },
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/move",
            json={
                "source": "/docs",
                "destination": "/notes/docs",
                "versions": {},
                "tokens": ["not-a-list-token", 1],
            },
        ).status_code
        == 400
    )
