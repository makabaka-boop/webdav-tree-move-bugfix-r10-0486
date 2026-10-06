"""端到端测试：两个客户端交错目录锁、子锁、到期重锁与同版本写入，
最后走一次页面保存流程。所有请求通过 HTTP 层发出（FastAPI TestClient）。
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
SHARED_LOCK_XML = LOCK_XML.replace("exclusive", "shared")


@pytest.fixture()
def env():
    clock = FakeClock()
    client = TestClient(create_app(db_path=":memory:", clock=clock))
    return clock, client


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


# ---------- 目录锁与子锁交错 ----------


def test_directory_lock_protects_descendants(env):
    """A 持目录 Depth infinity 锁时，B 既不能锁子文件，也不能绕过目录锁改写子文件。"""
    _, c = env
    etag_a = get(c, "/docs/a.txt").headers["etag"]

    token_a = lock_token(do_lock(c, "/docs", depth="infinity"))

    # B 对子文件加锁 → 冲突
    assert do_lock(c, "/docs/a.txt", depth="0").status_code == 423
    # B 无令牌 PUT 子文件 → 423，正文与版本不变
    r = do_put(c, "/docs/a.txt", "B 的篡改", etag=etag_a)
    assert r.status_code == 423
    g = get(c, "/docs/a.txt")
    assert g.text == "alpha\n" and g.headers["etag"] == etag_a

    # A 持令牌 + 正确 If-Match → 可写
    r = do_put(c, "/docs/a.txt", "A 的更新", etag=etag_a, if_header=f"(<{token_a}>)")
    assert r.status_code == 204
    assert get(c, "/docs/a.txt").text == "A 的更新"

    # A 解锁后 B 才能加子锁
    assert do_unlock(c, "/docs", token_a).status_code == 204
    lock_token(do_lock(c, "/docs/a.txt", depth="0"))


def test_descendant_lock_blocks_ancestor_depth_lock(env):
    """后代已有冲突锁时，祖先的 Depth infinity 锁必须失败；Depth 0 祖先锁不冲突。"""
    _, c = env
    token_b = lock_token(do_lock(c, "/docs/b.txt", depth="0"))

    assert do_lock(c, "/docs", depth="infinity").status_code == 423
    # Depth 0 只锁集合自身，不覆盖后代 → 允许
    lock_token(do_lock(c, "/docs", depth="0"))

    assert do_unlock(c, "/docs/b.txt", token_b).status_code == 204


def test_root_depth_lock_covers_everything(env):
    _, c = env
    token_root = lock_token(do_lock(c, "/", depth="infinity"))
    etag = get(c, "/readme.txt").headers["etag"]
    assert do_put(c, "/readme.txt", "x", etag=etag).status_code == 423
    assert do_lock(c, "/notes", depth="infinity").status_code == 423
    assert do_unlock(c, "/", token_root).status_code == 204


def test_unlock_token_must_match_resource(env):
    _, c = env
    token = lock_token(do_lock(c, "/docs", depth="infinity"))
    # 用祖先锁的令牌对后代路径 UNLOCK → 409，锁保留
    assert do_unlock(c, "/docs/a.txt", token).status_code == 409
    etag = get(c, "/docs/a.txt").headers["etag"]
    assert do_put(c, "/docs/a.txt", "x", etag=etag).status_code == 423
    assert do_unlock(c, "/docs", token).status_code == 204


# ---------- 到期、重锁与刷新 ----------


def test_expired_token_cannot_unlock_newer_lock(env):
    """锁到期后他人可重锁；过期令牌解不开后来建立的新锁。"""
    clock, c = env
    token_a = lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-30"))

    clock.advance(31)  # A 的锁到期
    token_b = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    assert token_b != token_a

    # 过期令牌 UNLOCK → 409，且 B 的锁不受影响
    assert do_unlock(c, "/docs/a.txt", token_a).status_code == 409
    etag = get(c, "/docs/a.txt").headers["etag"]
    assert do_put(c, "/docs/a.txt", "x", etag=etag).status_code == 423
    # 过期令牌的 PUT 同样无效
    assert (
        do_put(c, "/docs/a.txt", "x", etag=etag, if_header=f"(<{token_a}>)").status_code
        == 423
    )

    assert do_unlock(c, "/docs/a.txt", token_b).status_code == 204


def test_refresh_extends_without_creating_token(env):
    """刷新只延长有效期、返回同一令牌；未知令牌刷新失败且不产生新锁。"""
    clock, c = env
    token_a = lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-30"))

    r = c.request(
        "LOCK",
        "/dav/docs/a.txt",
        headers={"If": f"(<{token_a}>)", "Timeout": "Second-100"},
    )
    assert r.status_code == 200
    assert r.headers["lock-token"].strip("<>") == token_a  # 不创建新令牌

    clock.advance(50)  # 原 30s 已过，刷新后仍有效
    assert do_lock(c, "/docs/a.txt", depth="0").status_code == 423

    # 未知令牌刷新 → 412，锁状态不变
    r = c.request("LOCK", "/dav/docs/a.txt", headers={"If": "(<opaquelocktoken:nope>)"})
    assert r.status_code == 412
    assert "lock-token" not in r.headers
    assert do_lock(c, "/docs/a.txt", depth="0").status_code == 423

    clock.advance(60)  # 超过刷新后的 100s，锁真正到期
    lock_token(do_lock(c, "/docs/a.txt", depth="0"))


def test_expired_lock_cannot_be_refreshed(env):
    clock, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Second-10"))
    clock.advance(11)
    r = c.request("LOCK", "/dav/docs/a.txt", headers={"If": f"(<{token}>)"})
    assert r.status_code == 412
    # 刷新失败不会复活锁：他人可立即加锁
    lock_token(do_lock(c, "/docs/a.txt", depth="0"))


def test_infinite_timeout_never_expires(env):
    clock, c = env
    lock_token(do_lock(c, "/docs/a.txt", depth="0", timeout="Infinite"))
    clock.advance(10**7)
    assert do_lock(c, "/docs/a.txt", depth="0").status_code == 423


# ---------- 同版本写入与 If-Match ----------


def test_same_version_writes_are_serialized_by_etag(env):
    """两个客户端拿到同一版本；只有版本未变的写入可落盘，失败请求不改正文或版本。"""
    _, c = env
    etag_v1 = get(c, "/docs/a.txt").headers["etag"]  # A、B 读到同一版本

    token_a = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    r = do_put(c, "/docs/a.txt", "A 的修改", etag=etag_v1, if_header=f"(<{token_a}>)")
    assert r.status_code == 204
    etag_v2 = r.headers["etag"]
    assert etag_v2 != etag_v1
    assert do_unlock(c, "/docs/a.txt", token_a).status_code == 204

    # B 仍拿旧版本写入 → 412；正文与版本保持 A 的结果
    r = do_put(c, "/docs/a.txt", "B 的修改", etag=etag_v1)
    assert r.status_code == 412
    g = get(c, "/docs/a.txt")
    assert g.text == "A 的修改"
    assert g.headers["etag"] == etag_v2

    # 缺 If-Match → 428；弱 ETag / 通配 → 400
    assert do_put(c, "/docs/a.txt", "z").status_code == 428
    assert do_put(c, "/docs/a.txt", "z", etag="W/" + etag_v2).status_code == 400
    assert do_put(c, "/docs/a.txt", "z", etag="*").status_code == 400


# ---------- If 头语义 ----------


def test_if_header_or_and_semantics(env):
    _, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    etag = get(c, "/docs/a.txt").headers["etag"]

    # 多列表 OR：第一个列表失败、第二个成功 → 通过
    r = do_put(
        c,
        "/docs/a.txt",
        "or-ok",
        etag=etag,
        if_header=f"(<opaquelocktoken:bogus>) (<{token}>)",
    )
    assert r.status_code == 204
    etag = r.headers["etag"]

    # 同列表 AND：令牌正确但 ETag 条件不符 → 412
    r = do_put(
        c, "/docs/a.txt", "and-fail", etag=etag, if_header=f'(<{token}> ["999"])'
    )
    assert r.status_code == 412
    assert get(c, "/docs/a.txt").text == "or-ok"  # 失败不改正文

    # 同列表令牌 + 正确 ETag 条件 → 通过
    r = do_put(
        c, "/docs/a.txt", "etag-cond", etag=etag, if_header=f"(<{token}> [{etag}])"
    )
    assert r.status_code == 204


def test_if_header_rejects_not_and_tagged_lists(env):
    _, c = env
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    etag = get(c, "/docs/a.txt").headers["etag"]

    assert (
        do_put(
            c, "/docs/a.txt", "x", etag=etag, if_header=f"(Not <{token}>)"
        ).status_code
        == 400
    )
    assert (
        do_put(
            c,
            "/docs/a.txt",
            "x",
            etag=etag,
            if_header=f"<http://example.com/dav/docs/a.txt> (<{token}>)",
        ).status_code
        == 400
    )


# ---------- 方法与服务约束 ----------


def test_service_constraints(env):
    _, c = env
    # 集合只用于锁定
    assert c.get("/dav/docs").status_code == 405
    assert do_put(c, "/docs", "x", etag='"1"').status_code == 405
    lock_token(do_lock(c, "/docs", depth="0"))  # 集合可以加锁
    # 不存在的资源
    assert c.get("/dav/nope.txt").status_code == 404
    assert do_put(c, "/nope.txt", "x", etag='"1"').status_code == 404
    assert do_lock(c, "/nope.txt").status_code == 404
    # 不支持的方法
    for method in ("MKCOL", "MOVE", "COPY", "DELETE", "POST", "PROPFIND"):
        assert c.request(method, "/dav/docs/a.txt").status_code == 405
    # 锁约束：Depth 1、共享锁、坏 Timeout
    assert do_lock(c, "/docs/b.txt", depth="1").status_code == 400
    assert do_lock(c, "/docs/b.txt", body=SHARED_LOCK_XML).status_code == 400
    r = c.request(
        "LOCK",
        "/dav/docs/b.txt",
        content=LOCK_XML,
        headers={"Depth": "0", "Timeout": "tomorrow"},
    )
    assert r.status_code == 400


# ---------- 页面保存流程 ----------


def test_page_save_flow(env):
    """模拟编辑页：取得文件和 ETag → 锁定 → 编辑 → 保存 → 解锁；
    冲突时权威版本可重新拉取，本地草稿由页面保留。"""
    _, c = env
    files = c.get("/api/files").json()["files"]
    assert "/docs/a.txt" in files

    # 正常流程：GET → LOCK → PUT(If-Match + If) → UNLOCK
    g = get(c, "/docs/a.txt")
    etag = g.headers["etag"]
    token = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    r = do_put(
        c, "/docs/a.txt", g.text + "页面编辑\n", etag=etag, if_header=f"(<{token}>)"
    )
    assert r.status_code == 204
    assert do_unlock(c, "/docs/a.txt", token).status_code == 204

    # 冲突流程：两个页面会话基于同一版本
    s1 = get(c, "/docs/a.txt")
    s2 = get(c, "/docs/a.txt")
    assert s1.headers["etag"] == s2.headers["etag"]

    t1 = lock_token(do_lock(c, "/docs/a.txt", depth="0"))
    r = do_put(
        c, "/docs/a.txt", "会话一保存", etag=s1.headers["etag"], if_header=f"(<{t1}>)"
    )
    assert r.status_code == 204
    assert do_unlock(c, "/docs/a.txt", t1).status_code == 204

    # 会话二基于旧版本保存 → 412；权威版本仍是会话一的内容
    r = do_put(c, "/docs/a.txt", "会话二草稿", etag=s2.headers["etag"])
    assert r.status_code == 412
    g = get(c, "/docs/a.txt")
    assert g.text == "会话一保存"
    assert g.headers["etag"] != s2.headers["etag"]
