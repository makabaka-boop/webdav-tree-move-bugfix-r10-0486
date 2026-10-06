"""POST /api/move 端到端测试：整棵目录迁移的内容、版本、锁与原子性。

所有请求都通过真实 HTTP 接口（FastAPI TestClient）发出，
锁的到期时刻由注入的 FakeClock 精确控制。
"""

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

DOCS_FILES = ("/docs/a.txt", "/docs/b.txt")


@pytest.fixture()
def env():
    clock = FakeClock()
    client = TestClient(create_app(db_path=":memory:", clock=clock))
    return clock, client


def do_lock(client, path, depth="0", timeout="Second-300"):
    return client.request(
        "LOCK",
        "/dav" + path,
        content=LOCK_XML,
        headers={"Depth": depth, "Timeout": timeout, "Content-Type": "application/xml"},
    )


def lock_token(response):
    assert response.status_code == 200, response.text
    return response.headers["lock-token"].strip("<>")


def do_unlock(client, path, token):
    return client.request("UNLOCK", "/dav" + path, headers={"Lock-Token": f"<{token}>"})


def do_put(client, path, content, etag=None, if_header=None):
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if etag is not None:
        headers["If-Match"] = etag
    if if_header is not None:
        headers["If"] = if_header
    return client.put("/dav" + path, content=content, headers=headers)


def get(client, path):
    r = client.get("/dav" + path)
    assert r.status_code == 200
    return r


def snapshot(client):
    """当前全部文件的 {路径: (正文, ETag)} 快照。"""
    snap = {}
    for p in client.get("/api/files").json()["files"]:
        r = get(client, p)
        snap[p] = (r.text, r.headers["etag"])
    return snap


def manifest(client, *paths):
    return {p: get(client, p).headers["etag"] for p in paths}


def move(client, source, destination, versions, tokens=()):
    return client.post(
        "/api/move",
        json={
            "source": source,
            "destination": destination,
            "versions": versions,
            "tokens": list(tokens),
        },
    )


# ---------- 成功路径：整棵目录、正文、版本 ----------


def test_move_directory_preserves_tree_content_and_versions(env):
    """整棵目录迁移：文件、集合本身都到达新位置，正文与版本不因位置变化而改变。"""
    _, c = env
    etag_v1 = get(c, "/docs/a.txt").headers["etag"]
    assert do_put(c, "/docs/a.txt", "alpha v2\n", etag=etag_v1).status_code == 204

    before = snapshot(c)
    versions = {p: etag for p, (_, etag) in before.items() if p in DOCS_FILES}
    r = move(c, "/docs", "/notes/docs", versions)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert {m["from"] for m in body["moved"]} == {"/docs", *DOCS_FILES}
    assert {m["to"] for m in body["moved"]} == {
        "/notes/docs",
        "/notes/docs/a.txt",
        "/notes/docs/b.txt",
    }

    # 新路径的正文与版本和迁移前完全一致
    for old, new in (("/docs/a.txt", "/notes/docs/a.txt"), ("/docs/b.txt", "/notes/docs/b.txt")):
        g = get(c, new)
        assert g.text == before[old][0]
        assert g.headers["etag"] == before[old][1]
    # 集合本身也迁走了：新位置是集合，原位置什么都不剩
    assert c.get("/dav/notes/docs").status_code == 405
    assert c.get("/dav/docs").status_code == 404
    assert c.get("/dav/docs/a.txt").status_code == 404
    assert c.get("/dav/docs/b.txt").status_code == 404
    # 未迁移的资源不受影响
    assert snapshot(c) == {
        "/notes/docs/a.txt": before["/docs/a.txt"],
        "/notes/docs/b.txt": before["/docs/b.txt"],
        "/notes/todo.txt": before["/notes/todo.txt"],
        "/readme.txt": before["/readme.txt"],
    }


def test_move_nested_subtree_then_parent(env):
    """先迁入子树再整体迁走父目录：多级子目录随文件一起迁移。"""
    _, c = env
    assert move(c, "/docs", "/notes/docs", manifest(c, *DOCS_FILES)).status_code == 200

    before = snapshot(c)
    versions = {
        p: etag
        for p, (_, etag) in before.items()
        if p == "/notes/todo.txt" or p.startswith("/notes/docs/")
    }
    r = move(c, "/notes", "/archive", versions)
    assert r.status_code == 200, r.text

    g = get(c, "/archive/docs/a.txt")
    assert g.text == before["/notes/docs/a.txt"][0]
    assert g.headers["etag"] == before["/notes/docs/a.txt"][1]
    assert get(c, "/archive/todo.txt").text == before["/notes/todo.txt"][0]
    assert c.get("/dav/archive/docs").status_code == 405
    assert c.get("/dav/notes").status_code == 404
    assert c.get("/dav/notes/docs").status_code == 404


def test_move_single_file(env):
    _, c = env
    before = snapshot(c)
    r = move(c, "/readme.txt", "/notes/readme.txt", manifest(c, "/readme.txt"))
    assert r.status_code == 200, r.text
    g = get(c, "/notes/readme.txt")
    assert g.text == before["/readme.txt"][0]
    assert g.headers["etag"] == before["/readme.txt"][1]
    assert c.get("/dav/readme.txt").status_code == 404


def test_move_emptied_directory(env):
    """空目录也随迁移到达新位置，而不是留在原处。"""
    _, c = env
    assert move(c, "/notes/todo.txt", "/todo.txt", manifest(c, "/notes/todo.txt")).status_code == 200
    assert c.get("/dav/notes").status_code == 405  # /notes 现在是空集合

    r = move(c, "/notes", "/notes2", {})
    assert r.status_code == 200, r.text
    assert c.get("/dav/notes2").status_code == 405
    assert c.get("/dav/notes").status_code == 404


# ---------- 原子性：任一条件不满足，目录、文件和锁都保持原状 ----------


def test_stale_version_aborts_whole_batch(env):
    """批次中某个文件版本已过期：整批失败，没有任何文件被搬走。"""
    _, c = env
    before = snapshot(c)
    versions = manifest(c, *DOCS_FILES)
    versions["/docs/b.txt"] = '"999"'  # 过期版本
    r = move(c, "/docs", "/notes/docs", versions)
    assert r.status_code == 409
    assert r.json()["ok"] is False
    assert snapshot(c) == before
    assert c.get("/dav/notes/docs").status_code == 404


def test_incomplete_manifest_rejected(env):
    """版本清单不完整：请求被拒绝，目录保持原状。"""
    _, c = env
    before = snapshot(c)
    r = move(c, "/docs", "/notes/docs", manifest(c, "/docs/a.txt"))  # 缺 b.txt
    assert r.status_code == 400
    assert snapshot(c) == before


def test_source_lock_blocks_until_satisfied_then_migrates(env):
    """源目录的有效锁：未提交令牌整批失败且锁保持原状；提交后锁随目录迁移。"""
    _, c = env
    token = lock_token(do_lock(c, "/docs", depth="infinity"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before
    assert c.get("/dav/notes/docs").status_code == 404
    # 锁仍在原处保护
    etag = get(c, "/docs/a.txt").headers["etag"]
    assert do_put(c, "/docs/a.txt", "x", etag=etag).status_code == 423

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200
    # 锁随目录迁移：新路径仍受同一令牌保护
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 423
    assert (
        do_put(c, "/notes/docs/a.txt", "x", etag=etag, if_header=f"(<{token}>)").status_code
        == 204
    )
    # 旧路径不再是锁的归属，新路径才能解锁
    assert do_unlock(c, "/docs", token).status_code == 409
    assert do_unlock(c, "/notes/docs", token).status_code == 204


def test_descendant_lock_blocks_until_satisfied_then_migrates(env):
    """后代文件的有效锁：未提交令牌整批失败；提交后锁随文件迁移。"""
    _, c = env
    token = lock_token(do_lock(c, "/docs/b.txt", depth="0"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before
    assert do_unlock(c, "/docs/b.txt", token).status_code == 204  # 锁保持原状

    token = lock_token(do_lock(c, "/docs/b.txt", depth="0"))
    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200
    etag = get(c, "/notes/docs/b.txt").headers["etag"]
    assert do_put(c, "/notes/docs/b.txt", "x", etag=etag).status_code == 423
    assert do_unlock(c, "/notes/docs/b.txt", token).status_code == 204


def test_destination_parent_lock_must_be_satisfied(env):
    """目标父目录的有效锁（含 Depth 0）保护成员变更：未提交令牌整批失败。"""
    _, c = env
    token = lock_token(do_lock(c, "/notes", depth="0"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200
    # 父目录的锁不属于源子树，不随迁移移动
    assert do_unlock(c, "/notes", token).status_code == 204


def test_destination_ancestor_depth_lock_covers_moved_tree(env):
    """目标祖先的 Depth infinity 锁：必须满足；迁移后仍覆盖迁入的子树。"""
    _, c = env
    token = lock_token(do_lock(c, "/notes", depth="infinity"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200
    etag = get(c, "/notes/docs/a.txt").headers["etag"]
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 423
    assert (
        do_put(c, "/notes/docs/a.txt", "x", etag=etag, if_header=f"(<{token}>)").status_code
        == 204
    )
    assert do_unlock(c, "/notes", token).status_code == 204


def test_ancestor_lock_outside_source_must_be_satisfied(env):
    """源之外祖先的深度锁覆盖源：必须提交令牌，但锁本身不迁移。"""
    _, c = env
    token = lock_token(do_lock(c, "/", depth="infinity"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200
    assert do_unlock(c, "/", token).status_code == 204


def test_all_covering_lock_tokens_required(env):
    """同批两把有效锁只提交一把：整批失败，目录与锁都保持原状。"""
    _, c = env
    token_a = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    token_b = lock_token(do_lock(c, "/docs/b.txt", depth="0"))
    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token_a]).status_code == 409
    assert snapshot(c) == before

    assert (
        move(c, "/docs", "/notes/docs", versions, tokens=[token_a, token_b]).status_code
        == 200
    )


# ---------- 锁的剩余有效期与同一裁决时刻 ----------


def test_migrated_lock_keeps_remaining_lifetime(env):
    """锁随资源迁移后保留剩余有效期：既不提前失效，也不被重新续期。"""
    clock, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-300"))
    clock.advance(100)
    versions = manifest(c, *DOCS_FILES)
    assert move(c, "/docs", "/notes/docs", versions, tokens=[token]).status_code == 200

    clock.advance(150)  # 距建立共 250s < 300s：锁仍有效
    etag = get(c, "/notes/docs/a.txt").headers["etag"]
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 423

    clock.advance(60)  # 共 310s > 300s：锁按原到期时刻失效
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 204


def test_expired_locks_neither_block_nor_migrate(env):
    """过期锁不阻止迁移，也不随迁移复活。"""
    clock, c = env
    lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-30"))
    lock_token(do_lock(c, "/docs/b.txt", depth="0", timeout="Second-30"))
    clock.advance(31)

    versions = manifest(c, *DOCS_FILES)
    assert move(c, "/docs", "/notes/docs", versions).status_code == 200
    # 新路径无任何锁残留
    etag = get(c, "/notes/docs/a.txt").headers["etag"]
    assert do_put(c, "/notes/docs/a.txt", "x", etag=etag).status_code == 204


def test_batch_locks_judged_at_single_moment(env):
    """同一批操作中的锁按同一个时间点判断：过期的不算数，有效的按原剩余期迁移。"""
    clock, c = env
    lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-30"))
    token_b = lock_token(do_lock(c, "/docs/b.txt", depth="0", timeout="Second-60"))
    clock.advance(31)  # a.txt 的锁已过期，b.txt 的锁仍有效

    versions = manifest(c, *DOCS_FILES)
    before = snapshot(c)
    # 缺 b.txt 的有效锁令牌 → 整批失败
    assert move(c, "/docs", "/notes/docs", versions).status_code == 409
    assert snapshot(c) == before

    assert move(c, "/docs", "/notes/docs", versions, tokens=[token_b]).status_code == 200
    # b.txt 的锁按原剩余有效期（60 - 31 = 29s）迁移
    clock.advance(28)
    etag = get(c, "/notes/docs/b.txt").headers["etag"]
    assert do_put(c, "/notes/docs/b.txt", "x", etag=etag).status_code == 423
    clock.advance(2)  # 共 61s：到期
    assert do_put(c, "/notes/docs/b.txt", "x", etag=etag).status_code == 204


# ---------- 请求校验与资源约束 ----------


def test_move_validation_and_missing_resources(env):
    """根目录、自身/后代、点段、缺字段 → 400；状态冲突 → 409；树保持原状。"""
    _, c = env
    before = snapshot(c)
    versions = manifest(c, *DOCS_FILES)

    bad_requests = [
        {"source": "/", "destination": "/root2", "versions": {}, "tokens": []},
        {"source": "/docs", "destination": "/docs", "versions": versions, "tokens": []},
        {"source": "/docs", "destination": "/docs/sub", "versions": versions, "tokens": []},
        {"source": "/docs/../notes", "destination": "/x", "versions": {}, "tokens": []},
        {"source": "/docs", "destination": "/notes/./docs", "versions": versions, "tokens": []},
        {"source": "/docs", "destination": "/notes/docs"},  # 缺 versions/tokens
        {"source": "/docs", "versions": versions, "tokens": []},  # 缺 destination
    ]
    for payload in bad_requests:
        r = c.post("/api/move", json=payload)
        assert r.status_code == 400, (payload, r.text)
        assert r.json()["ok"] is False

    conflicts = [
        {"source": "/nope", "destination": "/x", "versions": {}, "tokens": []},
        {"source": "/docs", "destination": "/notes", "versions": versions, "tokens": []},
        {"source": "/readme.txt", "destination": "/notes/todo.txt",
         "versions": manifest(c, "/readme.txt"), "tokens": []},
        {"source": "/docs", "destination": "/nope/docs", "versions": versions, "tokens": []},
        {"source": "/docs", "destination": "/readme.txt/docs", "versions": versions, "tokens": []},
    ]
    for payload in conflicts:
        r = c.post("/api/move", json=payload)
        assert r.status_code == 409, (payload, r.text)
        assert r.json()["ok"] is False

    assert snapshot(c) == before
    assert c.get("/dav/docs").status_code == 405  # 原树保持原状
