# Skill Telemetry Server

接收 skill 调用埋点上报，并提供 Web 控制台查询。

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
- **查询 API**：`GET /stats/query?username=&skill=&start=&end=&group_by=user_skill&format=json`

## API

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/health` | GET | 健康检查 |
| `/track` | POST | 上报单条事件 |
| `/track/batch` | POST | 批量上报（client 补传积压事件用） |
| `/stats/query` | GET | 统一查询，支持筛选/聚合/CSV 导出 |
| `/stats/summary` | GET | 等价于 group_by=user_skill |
| `/stats/by_user` | GET | 按用户聚合 |
| `/stats/by_skill` | GET | 按 skill 聚合 |
| `/` | GET | Web 控制台 |

## 数据存储

默认在脚本同目录创建 `telemetry.db`（SQLite）。可通过环境变量 `TELEMETRY_DB` 指定路径：

```bash
TELEMETRY_DB=/var/data/telemetry.db uvicorn app:app --host 0.0.0.0 --port 8000
```

### 启动期数据库初始化策略

server 启动时按以下规则处理数据库：

| 情况 | 行为 |
| --- | --- |
| 数据库文件**不存在** | 自动创建空库并建表 |
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

## 表结构

```sql
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    skill           TEXT NOT NULL,
    hostname        TEXT,
    client_ts       TEXT NOT NULL,   -- 客户端 ISO8601 时间戳
    server_ts       TEXT NOT NULL,   -- 入库时间戳
    client_version  TEXT
);
```
