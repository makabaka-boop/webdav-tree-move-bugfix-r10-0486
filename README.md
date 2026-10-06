# 受限 WebDAV 文本工作区

SQLite 保存固定目录树、正文、ETag 与锁的极简 WebDAV 服务，附带轻量编辑页。
只支持已有资源的 **GET / PUT / LOCK / UNLOCK**；集合仅用于锁定。
不支持创建、移动、删除、共享锁与账号系统。

## 运行

```bash
docker compose up --build     # http://localhost:8080/ 为编辑页
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # DB_PATH 环境变量指定 SQLite 路径，默认内存库
python -m pytest                       # 运行测试
```

## 固定目录树

启动时播种，之后只改正文与版本，不增删节点：

```
/                 集合
├── docs/         集合
│   ├── a.txt
│   └── b.txt
├── notes/        集合
│   └── todo.txt
└── readme.txt
```

## 协议子集

| 方法 | 行为 |
| --- | --- |
| `GET /dav/<文件>` | 200 返回正文与强 ETag；集合 405；不存在 404 |
| `PUT /dav/<文件>` | 必须携带强 `If-Match`（缺失 428、不符 412）；必须满足所有覆盖锁（423）；成功 204 并返回新 ETag |
| `LOCK /dav/<路径>` | 仅独占写锁；`Depth: 0` 或 `infinity`（默认）；`Timeout: Second-N` / `Infinite`；无正文时按 `If` 头中的令牌刷新 |
| `UNLOCK /dav/<路径>` | `Lock-Token` 头指定要解的锁；成功 204 |

### 锁语义

- 祖先的 Depth infinity 锁保护全部后代；后代已有冲突锁时，祖先的深度锁失败（423）。
- 锁超时由可注入时钟裁决（生产 `SystemClock`，测试 `FakeClock`）；过期锁在事务内即时清除。
- 刷新只延长有效期、返回同一令牌，绝不创建新令牌；过期令牌无法刷新，也解不开后来建立的新锁。

### 条件头

- `If-Match`：仅单个强 ETag（`*`、弱 ETag 一律 400）。
- `If`：仅无标签条件列表，条件为正向锁令牌 `<opaquelocktoken:…>` 或 ETag `["…"]`；
  多个列表按 OR、同一列表内按 AND 求值；`Not` 与带标签列表明确不支持（400）。
- PUT 时 If 头中必须提交覆盖该资源的**所有**锁令牌，否则 423。

### 事务性

锁状态、到期判断、条件验证与正文更新在单笔 `BEGIN IMMEDIATE` 事务内裁决；
任何失败路径整体回滚，失败请求不改正文或版本号。

## 编辑页

`GET /` 提供单页编辑器：选择文件 → 加载（取得正文与 ETag）→ 锁定并编辑 →
保存（自动携带 `If-Match` 与 `If`）→ 解锁。保存被拒（412/423/428）时，
页面把本地草稿保留在编辑框并备份到 localStorage，同时拉取并展示服务器上的权威版本。

## 测试

`tests/test_webdav.py` 用两个客户端交错演练：目录深度锁保护子文件、后代锁阻止祖先深度锁、
到期重锁且过期令牌解不开新锁、刷新不创建新令牌、同版本写入由强 If-Match 串行化、
If 头 OR/AND 语义，以及一次完整的页面保存流程（含冲突时权威版本回读）。

`tests/test_move.py` 通过真实 HTTP 接口验证 `POST /api/move` 的整棵目录迁移：
成功时正文、版本与锁的剩余有效期原样保留并随资源到达新路径；
版本过期、清单不完整或锁未满足时整批失败，目录、文件与锁都保持原状。



## 目录迁移
POST /api/move，JSON 含 source、destination、versions（源子树每个文件路径对应强 ETag）和 tokens（所持锁令牌数组）。只把已有文件或完整目录迁入已存在集合下的空路径，不覆盖目标。目录本身及子目录随文件一起迁移；正文与 ETag 不因位置变化而改变。覆盖源或目标的有效锁都必须满足，源内锁保留令牌、深度和到期时刻并随对应资源迁移。过期锁按同一裁决时刻处理。失败整组保留原树和锁；禁止根目录、自身/后代、点段及不完整版本清单。迁移后普通 GET、PUT、LOCK、UNLOCK 使用新路径。
