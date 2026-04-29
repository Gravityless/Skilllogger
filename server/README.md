# Skill Telemetry Server

接收 skill 调用埋点上报，并提供 Web 控制台查询。FastAPI + SQLite 单文件部署，
启动期自动建库 / 建表 / 建索引；事件按 `event_id` 幂等去重，重传不会产生重复行。

## 安装

```bash
pip install fastapi uvicorn jinja2
```

## 启动

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

启动后：

- **Web 控制台**：浏览器访问 `http://<server-ip>:8000/`
  - 支持按用户名、skill 名（模糊匹配）、起止日期、聚合维度（用户 / Skill / 用户×Skill / 按天）筛选
  - KPI 卡片显示总调用次数、独立用户数、独立 Skill 数
  - 表格支持点击表头排序、分页
  - 可一键导出当前筛选结果为 CSV（Excel 直接打开，含 BOM 支持中文）
- **健康检查**：`GET /health`
- **查询 API**：`GET /stats/query?username=&skill=&start=&end=&group_by=user_skill&format=json&limit=1000`

## API

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/health` | GET | 健康检查，返回 `{status, time}` |
| `/track` | POST | 上报单条事件，返回 `{ok, received, inserted}` |
| `/track/batch` | POST | 批量上报（client 补传积压事件用），返回 `{ok, received, inserted}` |
| `/stats/query` | GET | 统一查询，支持筛选 / 聚合 / CSV 导出 |
| `/stats/summary` | GET | 等价于 `group_by=user_skill` |
| `/stats/by_user` | GET | 按用户聚合 |
| `/stats/by_skill` | GET | 按 skill 聚合 |
| `/` | GET | Web 控制台 |

### 上报请求体

`POST /track` 接受单个事件，`POST /track/batch` 接受 `{"events":[...]}`。事件字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | string (1–128) | ✓ | 用户名 |
| `skill` | string (1–256) | ✓ | skill 名 |
| `timestamp` | string | ✓ | 客户端 ISO8601 时间戳（UTC） |
| `event_id` | string (1–64) | ✓ | 客户端生成的事件唯一 ID，用于服务端幂等去重 |
| `hostname` | string (≤128) | – | 主机名 |
| `client_version` | string (≤32) | – | client 版本 |

服务端会补充 `server_ts`（入库时刻 UTC）。响应中 `received` 是收到的事件条数，
`inserted` 是真正落库的条数；`inserted < received` 表示部分事件因 `event_id` 已存在
被幂等丢弃（详见下文「幂等去重」）。

### `/stats/query` 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `username` | – | 模糊匹配（`LIKE %x%`） |
| `skill` | – | 模糊匹配（`LIKE %x%`） |
| `start` / `end` | – | 按 `client_ts` 字符串比较的起止时间，包含端点 |
| `group_by` | `user_skill` | `user` / `skill` / `user_skill` / `day` |
| `format` | `json` | `json` 返回 `{rows, kpis}`；`csv` 返回带 BOM 的 CSV 附件 |
| `limit` | `1000` | 1–100000，限制返回行数 |

## 幂等去重

`events.event_id` 上有 `UNIQUE` 约束，所有写入走 `INSERT OR IGNORE`。语义为
**effectively exactly-once**：

- client 在事件构造时生成稳定的 `event_id`（UUID），任何重传场景（网络超时、
  孤儿 sending 文件回收、ack 丢失）都复用同一 id；
- batch 内重复的 id 会被折叠，跨次重发的 id 会被静默丢弃；
- 接口仍返回 200，`inserted` 表示实际新增的行数，便于 client 观察。

## 数据存储

默认在脚本同目录创建 `telemetry.db`（SQLite）。可通过环境变量 `TELEMETRY_DB` 指定路径：

```bash
TELEMETRY_DB=/var/data/telemetry.db uvicorn app:app --host 0.0.0.0 --port 8000
```

### 启动期数据库初始化策略

server 启动时按以下规则处理数据库（实现见 `app.py:init_db`）：

| 情况 | 行为 |
| --- | --- |
| 数据库文件**不存在** | 自动创建空库并建表 / 建索引 |
| 数据库文件**已存在** | 直接复用，旧数据保留（`CREATE TABLE IF NOT EXISTS` 兼容旧 schema） |
| 启动时设置 `TELEMETRY_NEW_DB=1` 且旧库存在 | 先把旧库重命名为 `telemetry.db.bak.YYYYMMDD-HHMMSS` 备份，再创建空库 |

```bash
# 默认：复用现有 db
uvicorn app:app --host 0.0.0.0 --port 8000

# 强制新建数据库（旧库自动备份，不会丢数据）
TELEMETRY_NEW_DB=1 uvicorn app:app --host 0.0.0.0 --port 8000
```

启动日志会清晰提示当次模式，例如：
```
[telemetry] using existing db: /var/data/telemetry.db
[telemetry] created new db: /var/data/telemetry.db
[telemetry] reset db, backup -> /var/data/telemetry.db.bak.20240315-103045
```

**Windows 健壮性**：备份旧库时若 `rename` 因杀软 / SQLite 句柄延迟释放而失败，
会自动重试最多 30 次（每次间隔 0.5s），并在跨文件系统等情况下退回到
`shutil.copy2 + unlink`。所有手段都失败时，会打印 `WARN` 并**保留旧库**继续启动 ——
**宁可不重建也绝不丢数据**。

## 表结构

```sql
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    skill           TEXT NOT NULL,
    hostname        TEXT,
    client_ts       TEXT NOT NULL,            -- 客户端 ISO8601 时间戳 (UTC)
    server_ts       TEXT NOT NULL,            -- 服务端入库时间戳 (UTC)
    client_version  TEXT,
    event_id        TEXT NOT NULL UNIQUE      -- 客户端生成的 UUID，幂等去重
);

CREATE INDEX idx_events_user_skill ON events(username, skill);
CREATE INDEX idx_events_skill      ON events(skill);
CREATE INDEX idx_events_user       ON events(username);
CREATE INDEX idx_events_client_ts  ON events(client_ts);
```

四个索引覆盖控制台主要查询模式：用户×skill 聚合、按 skill / 按用户筛选、
按时间范围筛选。
