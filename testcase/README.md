# Skilllogger 测试套件

使用 Python `unittest` 编排，覆盖 server 启动逻辑、server 端去重幂等性，以及三端
client（Python / bash / PowerShell）的全部关键场景。共 **30** 个用例，跨
Linux / macOS / Windows 三平台 CI 全绿。

## 目录结构

```
testcase/
├── README.md                 # 本文件
├── _client_base.py           # client 测试 mixin（C1-C8 通用场景）
├── common/
│   ├── server_fixture.py     # 启停隔离的 telemetry server（随机端口、临时 DB）
│   └── client_runner.py      # 跨平台调用 client 脚本（环境变量重定向 queue 目录）
├── test_server_db_init.py    # server 启动 DB 初始化（S1/S2/S3）
├── test_server_dedup.py      # server 端 event_id 幂等去重（S4/S5）
├── test_client_bash.py       # bash client 子类壳（C1-C8）
├── test_client_ps.py         # PowerShell client 子类壳（C1-C8）
└── test_client_python.py     # Python client 子类壳（P1-P8 复用 C1-C8）+ P9 零阻塞
```

## 用例命名约定

| 前缀 | 含义 | 文件 |
| ---- | ---- | ---- |
| `S*` | Server 端测试 | `test_server_*.py` |
| `C*` | Client 端通用场景（三端共享一套用例） | `_client_base.py` 内 mixin |
| `P*` | Python client 专属测试（P1-P8 复用 C1-C8；P9 零阻塞） | `test_client_python.py` |

子类（`test_client_bash.py` / `test_client_ps.py` / `test_client_python.py`）只声明
`CLIENT_KIND` 与 `QUEUE_SUBDIR`，真正的测试方法都在 `_client_base.py` 的 mixin 里
—— 一次编写，三端复用。

## 前置条件

- Python 3.8+
- 安装 server 依赖：`pip install fastapi uvicorn jinja2`
- bash client 测试需要：`bash`、`curl`
- PS client 测试需要：`pwsh`（任意平台）或 `powershell`（Windows）；缺失则该套件自动 skip
- Python client 测试需要：`python3`（与运行测试本身相同的解释器，三平台默认都有）

## 运行

```bash
# 全部
python -m unittest discover -s testcase -v

# 单文件
python -m unittest testcase.test_server_db_init -v
python -m unittest testcase.test_server_dedup   -v
python -m unittest testcase.test_client_bash    -v
python -m unittest testcase.test_client_ps      -v
python -m unittest testcase.test_client_python  -v
```

## 场景说明

### Server 启动 DB 初始化（`test_server_db_init.py`）

| ID | 场景 | 期望 |
| --- | --- | --- |
| S1 | DB 文件不存在 | 启动时自动创建空库与 `events` 表 |
| S2 | DB 文件已存在且含数据 | 默认启动应保留旧数据 |
| S3 | 启动时设置 `TELEMETRY_NEW_DB=1` | 旧库被备份为 `telemetry.db.bak.<时间戳>`，新库为空 |

### Server 端去重（`test_server_dedup.py`）

| ID | 场景 | 期望 |
| --- | --- | --- |
| S4 | 同 `event_id` 调 `/track` 两次 | 仅首次入库；第二次响应 `inserted=0` |
| S5 | `/track/batch` 含 batch 内重复 + 跨次重发 | batch 内折叠 + 跨次去重，最终行数稳定 |

去重原理：`events.event_id TEXT NOT NULL UNIQUE` + `INSERT OR IGNORE`。
（`event_id` 由 client 在事件构造时生成 UUID，重传永远复用同一 id。）

### Client 通用场景（三端各一套，C1-C8 / P1-P8）

| ID | 场景 | 期望 |
| --- | --- | --- |
| C1 | server 在线，调用 1 次 | server 收到 1 条 |
| C2 | server 离线，调用 1 次 | 落到本地 `queue.jsonl` |
| C3 | 离线累积后 server 恢复 | 下次调用补传积压 + 当前事件，队列清空 |
| C4 | 旧的 `queue.sending.*.jsonl`（mtime > 60s） | 启动期回收并上报，文件被删除 |
| C5 | 新的 `queue.sending.*.jsonl`（mtime < 60s） | 不被误回收 |
| C6 | 不传 `SkillName` | exit 0，server 无新事件 |
| C7 | 队列中混入损坏 JSON 行 | 不崩溃，跳过坏行，好行正常上报 |
| C8 | 同一 `event_id` 端到端重传 | server 仅入库一次（exactly-once） |

### Python client 专属（`test_client_python.py`）

| ID | 场景 | 期望 |
| --- | --- | --- |
| P9 | server 不可达时父进程 wallclock | < 1.5s（实测通常 < 200ms），验证 fire-and-forget 真正零阻塞 |

> Python client 是 fire-and-forget：父 `subprocess.run` 返回 ≠ 事件入库，因为
> HTTP I/O 在 detached 子进程里跑。`run_python_client` 默认通过
> `SKILL_TELEMETRY_WORKER_DONE_FILE` 环境变量让 worker 在结束时写一个标记
> 文件，runner 轮询等到该文件再返回，从而让既有 C1-C8 断言可原样复用。
> P9 主动关闭这个等待开关 (`wait_for_worker=False`)，纯测父进程耗时。

## 设计要点 / 隔离机制

每个用例都做到**完全隔离**，可放心并发执行：

1. **临时目录 (tmpdir)**：每个用例独立 `tempfile.TemporaryDirectory`；DB、queue 都置于其下。
2. **随机端口**：`socket.bind(("127.0.0.1", 0))` 让内核分配空闲端口，避免冲突。
3. **环境变量重定向 queue 目录**：
   - bash / Python (Unix) client 读 `$XDG_CACHE_HOME/skill-telemetry`
   - PS / Python (Windows) client 读 `$LOCALAPPDATA/SkillTelemetry`

   测试 fixture 把这两个变量指到临时目录里，client 写出的 queue 完全在测试控制之下，
   不会污染开发者本机的真实缓存目录，也避免并发用例之间互相干扰。
4. **server 以子进程启动真正的 uvicorn**（而非 import `app` 函数调用），确保
   `@app.on_event("startup")` 钩子（即 `init_db`）被触发，端到端覆盖启动逻辑。
5. **Python client fire-and-forget 同步化**：`SKILL_TELEMETRY_WORKER_DONE_FILE`
   测试桥（仅在该环境变量被设置时才生效，生产无任何副作用）让 worker 结束时
   touch 标记文件；runner 轮询该文件直到出现或超时，再交还控制权。
6. **Windows 兼容**：
   - sqlite3 connection 必须显式 `conn.close()`（`with sqlite3.connect(...)` 上下文
     只 commit/rollback 不关闭，未关闭句柄会阻塞后续 rename / unlink）；
   - server 子进程 stop 后 sleep 0.5s 让内核释放 SQLite 文件句柄；
   - `TemporaryDirectory(ignore_cleanup_errors=True)`（Python 3.10+）容忍清理时占用错误。

## CI 覆盖

GitHub Actions 工作流跨三平台 matrix 跑全套测试（仓库 `.github/workflows/tests.yml`）：

| Runner | bash client | PS client (`pwsh`) | Python client | server S* |
| ------ | ----------- | ------------------ | ------------- | --------- |
| `ubuntu-latest`  | ✓ | ✓ (apt 装 pwsh) | ✓ | ✓ |
| `macos-latest`   | ✓ | ✓ (brew 装 pwsh) | ✓ | ✓ |
| `windows-latest` | ✓ (Git Bash) | ✓ (powershell + pwsh) | ✓ | ✓ |

任一平台失败都会阻塞合入。
