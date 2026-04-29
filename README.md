# Skilllogger · Skill 调用埋点项目

一个轻量的 **C/S 架构** 数据埋点工具，用于统计「**哪些用户调用了哪些 skill、调用了多少次**」。

- **Client**：放进每个 skill 的 `scripts/` 目录，由大模型在执行 skill 完毕后静默调用
- **Server**：FastAPI + SQLite，提供上报 API、查询 API 和 Web 控制台
- **离线容错**：server 不可达时事件本地落队列，下次调用自动补传
- **跨平台 / 真正零阻塞**：**首选 Python client**（fire-and-forget，调用方典型 < 100ms 返回）；同时保留 PowerShell（Windows）+ bash（macOS / Linux）旧脚本作为 fallback

---

## 项目结构

```
Skilllogger/
├── SKILL_LOGGER.md                # 复制到 skill 的 SKILL.md 的引导文案
├── scripts/
│   ├── telemetry_client.py        # ⭐ Python client（推荐首选，跨平台，真正零阻塞）
│   ├── telemetry_client.ps1       # 旧方案：Windows client (PS 5.1 / 7+)
│   └── telemetry_client.sh        # 旧方案：macOS / Linux client (bash + curl)
├── server/
│   ├── app.py                     # FastAPI 服务（API + 控制台 + 启动期 init_db）
│   ├── README.md                  # server 端详细说明
│   └── templates/
│       └── dashboard.html         # Web 控制台页面
├── testcase/                      # 集成测试 (unittest)
│   ├── common/                    # 服务端 fixture / 三端 client runner
│   ├── test_client_bash.py        # bash client C1–C8
│   ├── test_client_ps.py          # pwsh client C1–C8
│   ├── test_client_python.py      # python client P1–P8 (=C1-C8) + P9 零阻塞
│   ├── test_server_db_init.py     # server S1–S3
│   └── test_server_dedup.py       # server S4–S5
└── .github/workflows/tests.yml    # CI: ubuntu / macos / windows × py3.11
```

---

## 一、Server 端（服务支持部门部署一次）

### 1. 安装依赖

```bash
cd server
pip install fastapi uvicorn jinja2
```

### 2. 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

启动后：

| 路径 | 说明 |
| --- | --- |
| `http://<server>:8000/` | **Web 控制台**（筛选 / 聚合 / KPI / 导出 CSV） |
| `http://<server>:8000/health` | 健康检查 |
| `http://<server>:8000/stats/query?...` | 统一查询 API |

数据存储在 `server/telemetry.db`（SQLite，自动创建）。可通过环境变量自定义路径：

```bash
TELEMETRY_DB=/var/data/telemetry.db uvicorn app:app --host 0.0.0.0 --port 8000
```

### 3. 让 client 知道 server 地址

各 client 默认上报到 `http://localhost:8000`。请通过环境变量在用户机器上覆盖：

```powershell
# Windows，可写入系统环境变量
setx SKILL_TELEMETRY_URL "http://your-server.intra.company.com:8000"
```

```bash
# macOS / Linux，写入 ~/.bashrc 或 ~/.zshrc
export SKILL_TELEMETRY_URL="http://your-server.intra.company.com:8000"
```

---

## 二、Skill 开发者集成步骤

### Step 1：复制 client 脚本进自己的 skill

把 `scripts/telemetry_client.py`（**推荐首选**）复制到你的 skill 的 `scripts/` 目录下。
也可以同时复制 `.ps1` / `.sh` 作为 fallback（不强制）。

```
your-skill/
├── SKILL.md
└── scripts/
    ├── telemetry_client.py        # ⭐ 推荐：跨平台，真正零阻塞
    ├── telemetry_client.ps1       # 可选 fallback（旧方案，会阻塞最长 ~6s）
    └── telemetry_client.sh        # 可选 fallback（旧方案，会阻塞最长 ~6s）
```

### Step 2：在 SKILL.md 末尾插入引导文案

打开 `SKILL_LOGGER.md`，把里面的内容复制粘贴到你的 `SKILL.md` 执行流程起始或末尾。

文案的核心是引导大模型在每次执行 skill 主流程完毕后**静默执行一次** client 脚本：

```bash
python3 scripts/telemetry_client.py "your_skill_name"
```

> Windows 上可用 `py -3` 或 `python` 替代 `python3`。
>
> 旧方案（不推荐，仅在没有 `python3` 的环境下使用）：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/telemetry_client.ps1" -SkillName "your_skill_name"
> ```
>
> ```bash
> bash scripts/telemetry_client.sh "your_skill_name"
> ```

> **重要**：脚本被设计为**完全静默 + 永远 exit 0**，不会污染大模型上下文，也绝不会中断 skill 主流程。失败会被默默吞掉并把事件落到本地队列。Python 版还额外做到**调用方零阻塞**（fire-and-forget，父进程 < 100ms 退出），旧 PS / bash 在 server 不可达时最坏阻塞 ~6s。

### Step 3：完成

下次用户调用你的 skill，事件就会自动上报到 server，并在控制台中可见。

---

## 三、查看数据

浏览器打开 `http://<server>:8000/`：

- **筛选**：用户名（模糊）、Skill 名（模糊）、起止日期
- **聚合维度**：用户 × Skill / 按用户 / 按 Skill / 按天
- **KPI 卡片**：总调用次数、独立用户数、独立 Skill 数（受筛选条件影响）
- **表格**：点击表头排序、分页
- **一键导出**当前筛选结果为 CSV（含 BOM，Excel 直接打开支持中文）

也可以直接调 API：

```bash
# 按用户 × skill 聚合（JSON）
curl 'http://server:8000/stats/query?group_by=user_skill'

# 模糊筛选 + 日期范围 + 导出 CSV
curl -OJ 'http://server:8000/stats/query?username=alice&skill=weather&start=2026-01-01&end=2026-12-31&group_by=day&format=csv'
```

---

## 四、数据模型

```sql
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    skill           TEXT NOT NULL,
    hostname        TEXT,
    client_ts       TEXT NOT NULL,   -- 客户端 ISO8601 时间戳 (UTC)
    server_ts       TEXT NOT NULL,   -- 服务端入库时间戳 (UTC)
    client_version  TEXT,
    event_id        TEXT NOT NULL UNIQUE  -- 客户端生成的 UUID，用于服务端幂等去重
);
```

每次 skill 调用 = 一条明细记录，便于后续任意维度的聚合分析。`event_id` 由 client 在事件第一次构造时生成，重传时**复用同一 id**；服务端 `INSERT OR IGNORE` 保证同一 `event_id` 永远只入库一次。

---

## 五、跨用户环境兼容性

Client 脚本经过特别设计，确保在任何用户机器上都能跑：

- **Python（推荐）**：仅依赖 Python 3 标准库，零第三方包；跨平台一份代码同时支持 Windows / macOS / Linux；用 `os.environ['USER'] || 'USERNAME'` + `getpass.getuser()` 三层 fallback 拿用户名；缓存目录跟随平台（`%LOCALAPPDATA%\SkillTelemetry` 或 `$XDG_CACHE_HOME/skill-telemetry`）；通过 `subprocess.Popen(start_new_session=True)`（Unix）/ `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW`（Windows）spawn detached 子进程，**父进程对调用方真正零阻塞（典型 < 100ms）**
- **Windows（旧 PS）**：兼容 PowerShell 5.1（Win10 默认）和 PowerShell 7+；用 `-ExecutionPolicy Bypass` 启动绕过本地策略；路径用 `$env:LOCALAPPDATA` 兼容中文用户名
- **macOS / Linux（旧 bash）**：仅依赖 bash + curl；用户名 fallback 到 `id -un`；路径用 `$XDG_CACHE_HOME` / `$HOME/.cache`
- **网络**：3 秒超时，跳过系统代理，避免被防火墙/代理拖慢
- **离线**：失败事件落 `%LOCALAPPDATA%\SkillTelemetry\queue.jsonl`（Windows）或 `~/.cache/skill-telemetry/queue.jsonl`（Unix），下次调用时优先批量补传。三种 client **共享同一队列目录**，可在同一台机器上混用
- **并发**：用原子重命名（`rename` / `Move-Item` / `os.rename`）认领待发送批次和孤儿文件，配合 append-only 写入主队列，避免多个 skill 并发时**重复上报**或**撕裂行**
- **静默**：所有错误吞掉，永远 `exit 0`，绝不打断 skill 主流程

---

## 六、工作原理详解

> 这一节面向想了解内部细节、或要做二次开发 / 排障的同学。

### 6.1 客户端两种执行模型

**Python client（推荐）—— Fire-and-forget 父子分工**

```
caller (LLM/skill) ──> Parent (前台, < 100ms)
                        1. 构造 event JSON
                        2. 原子 append 到 queue.jsonl
                        3. spawn detached 子进程
                        4. exit 0  ◀──── 调用方在这里就拿到返回, 不再等
                                        ▼
                       Child  (后台, 完全脱离父会话)
                        - Step 0  孤儿 sending 文件回收
                        - Step 1  抢占 queue → POST /track/batch → 失败回滚
                        - 静默退出
```

关键点：
- 父子之间通过 `queue.jsonl` 解耦：事件**先落盘再 spawn**，child 哪怕被杀也不丢；同时 child 的唯一职责就是消费 queue，逻辑简单，重启自动收敛。
- Detached spawn 跨平台：Unix `start_new_session=True` + 三标准流 `DEVNULL`；Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`。
- 父进程不做任何网络调用，**调用方 wallclock 与 server 在线状态完全解耦**。

**bash / PS client（旧方案）—— 同步四步状态机**

```
┌──────────────────────────────────────────────────────────────────┐
│  Step 0  孤儿回收  ──>  Step 1  补传历史队列  ──>  Step 2  上报  │
│      ▲                       │ 失败                  │ 失败       │
│      │                       └──┐                    │            │
│      │                          ▼                    ▼            │
│   sending.*                queue.jsonl          Step 3 入队      │
│   超过 60s                 (append 还原)        (append 单行)    │
└──────────────────────────────────────────────────────────────────┘
```

`bash` 与 `pwsh` 客户端骨架对称，都把整段逻辑包在 `try` / 子 shell 内并强制 `exit 0`，**任何异常都不会传出**；但同步串行 POST 在 server 不可达时**会阻塞调用方最长约 6 秒**（两次 3s 超时）。

**Step 0 — 孤儿回收**：扫描 `queue.sending.*.jsonl`。若 `mtime` 超过 60s 视为前一个被强杀的进程残留，**先用 `mv` 原子认领** 成自己专属的 `queue.sending.recover.<pid>.<ts>.jsonl`；认领成功才 append 回主队列再删除。

> ⚠️ 如果 Step 0 不做"先认领后处理"，多个 client 同时启动时会**同时 cat 同一个孤儿到 queue**，造成事件重复上报。当前实现用 `rename` 的原子性保证：只有一个并发实例能 mv 成功，其它静默跳过。

**Step 1 — 补传历史队列**：若主队列非空，把 `queue.jsonl` **原子 rename** 成 `queue.sending.<id>.jsonl`，解析为 `{"events":[...]}` 整批 POST `/track/batch`。
- 成功 → 删 sending 文件
- 失败 → 把 sending 内容 **append**（不是覆盖）回 queue.jsonl，保护期间 Step 3 写入的新事件

**Step 2 — 上报当前事件**：直接 POST `/track`，3 秒超时，跳过系统代理。

**Step 3 — 失败入队**：Step 2 失败时，把当前事件作为单行 JSON **append** 到 queue.jsonl。POSIX `O_APPEND` / .NET `AppendAllText` 保证多进程并发追加不会撕裂行。

> Python client 的 worker 子进程内部也使用相同的 Step 0 + Step 1 算法消费队列。三端共享同一 `queue.jsonl`、同一 `event_id` 协议，**可以在同一台机器上混用**。

### 6.2 关键不变式

| 不变式 | 实现机制 |
|---|---|
| **绝不丢事件** | 失败必入本地 JSONL 队列；下次任意调用都会优先补传 |
| **绝不重复入库（exactly-once）** | client 在事件构造时生成稳定的 `event_id`（UUID），重传永远复用同一 id；server 端 `INSERT OR IGNORE` + `UNIQUE` 约束 → 同一 id 仅入库 1 次 |
| **绝不打断 skill** | 全部 client 强制 `exit 0`：python 全局 try/except；bash 子 shell + `set +e`；pwsh `$ErrorActionPreference='SilentlyContinue'` |
| **真正零阻塞（仅 Python）** | 父进程不发 HTTP，落盘 + spawn detached 后立即退出，wallclock 与 server / 网络状态完全解耦 |
| **3 秒内必返回（bash / PS）** | curl `--max-time 3` / HttpWebRequest `Timeout=3000`；显式禁用代理避免被代理拖慢；最坏 ~6s（两次串行 POST） |
| **多实例并发不重复消费队列** | 队列消费用 rename 原子认领；新事件用 append-only |
| **跨用户名兼容** | python: `USER` → `USERNAME` → `getpass.getuser()`；bash: `$USER` → `id -un` → `unknown`；pwsh: `$env:USERNAME`；缓存路径基于 `$XDG_CACHE_HOME` / `$LOCALAPPDATA`，兼容中文用户名 |
| **跨平台时间戳** | python `datetime.now(timezone.utc).strftime(...)` 毫秒精度；bash 优先 `%3N`，BSD/macOS 不支持时回退秒级；pwsh `Get-Date.ToUniversalTime()` |
| **三端共享队列** | 同一台机器上三种 client 写同一 `queue.jsonl`，可任意混用；event_id 跨 client 一致协议 |

### 6.3 服务端启动期：`init_db()`

`server/app.py` 用 FastAPI 的 `@app.on_event("startup")` 触发，按 `TELEMETRY_NEW_DB` 环境变量分三态：

| 条件 | 行为 |
|---|---|
| `TELEMETRY_NEW_DB=1` 且旧库存在 | rename 旧库为 `telemetry.db.bak.<时间戳>` 备份后建新库 |
| 旧库不存在 | 创建空库 + 建表 |
| 旧库存在（默认） | 直接复用，`CREATE TABLE IF NOT EXISTS` 兼容老库 |

**Windows 健壮性**：rename 配 30×0.5s 重试 + `shutil.copy2/unlink` fallback；最终仍失败时打 `WARN` 并**保留旧库**继续启动 — **宁可不重建也绝不丢数据**。建表后建立 4 个索引：`(username,skill)`、`skill`、`username`、`client_ts`，对应主要查询模式。

### 6.4 服务端 API

| 路径 | 调用方 | 说明 |
|---|---|---|
| `POST /track` | client Step 2 | 单事件入库；返回 `{ok, received, inserted}`。`inserted=0` 表示该 `event_id` 已存在，被幂等丢弃 |
| `POST /track/batch` | client Step 1 | `{events:[...]}` 整批入库；同样返回 `inserted` 真正落库的条数 |
| `GET /health` | 监控/CI | `{"status":"ok",...}` |
| `GET /stats/query` | 控制台 / 脚本 | 统一聚合查询，支持 `group_by` ∈ {user, skill, user_skill, day}、`username/skill` 模糊筛选、`start/end` 时间范围、`format=csv` 导出 |
| `GET /stats/summary` `/by_user` `/by_skill` | 便捷接口 | 是 `stats_query` 不同 `group_by` 的快捷封装 |
| `GET /` | 浏览器 | Web 控制台 |

事件入库前由 Pydantic 校验：`username` `skill` `timestamp` `event_id` 均必填（`event_id` 长度 1–64）；`server_ts` 由服务端补充为入库时刻 UTC。

### 6.5 失败模式 & 数据保证总结

| 场景 | 行为 |
|---|---|
| Server 离线 | 事件落本地队列；下次任一 skill 调用时整批补传 |
| Server 5xx | 同上，client 把 sending 文件 append 回 queue |
| Client 进程被强杀（**rm sending 之前**） | sending 文件残留为孤儿；下一次 Step 0 在 60s 后回收并重传，server 按 `event_id` 幂等去重，**不会重复入库** |
| Server 已写库但 ack 丢失 | client 视为失败 → 入队 → 下次重传 → server 端 `event_id` 已存在 → 静默丢弃，**最终一致** |
| 多 client 同时跑 Step 0 | rename 原子认领，只有一个能领走，其它跳过；即便认领后再被强杀，sending 文件也会在下一轮被再次回收，最终通过 `event_id` 收敛 |
| 多 client 同时 Step 3 入队 | append-only，POSIX/Win API 保证单行原子 |
| Server 启动时旧库被锁 | rename 重试 15s + copy/unlink fallback；最终保留旧库 |
| 队列里有损坏行 | 解析跳过，正常行照常上报 |

> **数据保证**：在 `event_id` + `INSERT OR IGNORE` 的双重保护下，传输语义为 **effectively exactly-once** —— 事件可能被网络/进程异常重发若干次，但最终在数据库里**恰好出现一次**。

---

## 七、常见问题

**Q：Server 离线了怎么办？会丢数据吗？**
A：不会。client 把失败事件写入本地 JSONL 队列，下次任意一个 skill 调用时会先尝试批量补传。

**Q：脚本执行会不会让用户看到弹窗或乱码？**
A：不会。脚本全程静默，无任何 stdout/stderr 输出，也不会弹任何窗口。

**Q：如何修改 server 地址？**
A：在用户机器上设置环境变量 `SKILL_TELEMETRY_URL` 即可，无需改 client 脚本。

**Q：要不要鉴权？**
A：当前版本假设公司内网信任环境，未做鉴权。如需添加，可在 server 加 token header 校验，client 端从环境变量读取并发送。

**Q：如何重置数据库？**
A：启动 server 时设置环境变量 `TELEMETRY_NEW_DB=1`，旧库会自动备份为 `telemetry.db.bak.<时间戳>`，详见 `server/README.md`。

---

## 八、测试

仓库内置基于 Python `unittest` 的测试套件，覆盖：

- Server 启动期 DB 三态判断（无库建库 / 有库保留 / `TELEMETRY_NEW_DB=1` 强制新建并备份）
- Server 端 `event_id` 幂等去重（单事件 + batch 内 / 跨 batch）
- 三端 client（Python / bash / PS）八种通用场景（在线、离线入队、离线后补传、孤儿 sending 文件回收、新 sending 文件不被误回收、缺参数静默退出、损坏 JSON 行跳过、`event_id` 端到端去重）
- Python client 专属的零阻塞耗时验证（P9）

合计 **30** 个用例，跨 Linux / macOS / Windows 三平台 CI 全绿。运行：

```bash
pip install fastapi uvicorn jinja2
python -m unittest discover -s testcase -v
```

详见 [`testcase/README.md`](testcase/README.md)。

---

## License

Internal use within the company.
