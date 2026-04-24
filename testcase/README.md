# Skilllogger 测试套件

使用 Python `unittest` 编排，覆盖 server 启动逻辑与两端 client（bash / PowerShell）的全部关键场景。

## 目录结构

```
testcase/
├── README.md                 # 本文件
├── _client_base.py           # client 测试 mixin（C1-C7 通用场景）
├── common/
│   ├── server_fixture.py     # 启停隔离的 telemetry server（随机端口、临时 DB）
│   └── client_runner.py      # 跨平台调用 client 脚本
├── test_server_db_init.py    # server 启动 DB 初始化（S1/S2/S3）
├── test_client_bash.py       # bash client（C1-C7）
└── test_client_ps.py         # PowerShell client（C1-C7）
```

## 前置条件

- Python 3.8+
- 安装 server 依赖：`pip install fastapi uvicorn jinja2`
- bash client 测试需要：`bash`、`curl`
- PS client 测试需要：`pwsh`（任意平台）或 `powershell`（Windows）；缺失则该套件自动 skip

## 运行

从仓库根目录或 `testcase/` 目录运行：

```bash
# 全部
python -m unittest discover -s testcase -v

# 仅 server DB 初始化测试
python -m unittest testcase.test_server_db_init -v

# 仅 bash client
python -m unittest testcase.test_client_bash -v

# 仅 PS client
python -m unittest testcase.test_client_ps -v
```

亦可直接运行单个文件：

```bash
python testcase/test_server_db_init.py -v
python testcase/test_client_bash.py -v
python testcase/test_client_ps.py -v
```

## 场景说明

### Server 启动（`test_server_db_init.py`）
| ID | 场景 | 期望 |
| --- | --- | --- |
| S1 | DB 文件不存在 | 启动时自动创建空库与表 |
| S2 | DB 文件已存在且含数据 | 默认启动应保留旧数据 |
| S3 | 启动时设置 `TELEMETRY_NEW_DB=1` | 旧库被备份为 `telemetry.db.bak.<时间戳>`，新库为空 |

### Client（PS / bash 通用 C1-C7）
| ID | 场景 | 期望 |
| --- | --- | --- |
| C1 | server 在线，调用 1 次 | server 收到 1 条 |
| C2 | server 离线，调用 1 次 | 落到本地 `queue.jsonl` |
| C3 | 离线累积后 server 恢复 | 下次调用补传积压 + 当前事件，队列清空 |
| C4 | 旧的 `queue.sending.*.jsonl`（mtime > 60s） | 启动期回收并上报，文件被删除 |
| C5 | 新的 `queue.sending.*.jsonl`（mtime < 60s） | 不被误回收 |
| C6 | 不传 `SkillName` | exit 0，server 无新事件 |
| C7 | 队列中混入损坏 JSON 行 | 不崩溃，跳过坏行，好行正常上报 |

每个测试用例会启动一个隔离的 server（独立端口 + 临时 DB + 临时 queue 目录），互不影响，可并行执行。
