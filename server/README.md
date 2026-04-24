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
