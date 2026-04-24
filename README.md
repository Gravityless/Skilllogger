# Skilllogger · Skill 调用埋点项目

一个轻量的 **C/S 架构** 数据埋点工具，用于统计「**哪些用户调用了哪些 skill、调用了多少次**」。

- **Client**：放进每个 skill 的 `scripts/` 目录，由大模型在执行 skill 完毕后静默调用
- **Server**：FastAPI + SQLite，提供上报 API、查询 API 和 Web 控制台
- **离线容错**：server 不可达时事件本地落队列，下次调用自动补传
- **跨平台**：Windows（PowerShell）+ macOS / Linux（bash）双端 client

---

## 项目结构

```
Skilllogger/
├── SKILL_片段.md                  # 复制到 skill 的 SKILL.md 的引导文案
├── scripts/
│   ├── telemetry_client.ps1       # Windows client (PS 5.1 / 7+)
│   └── telemetry_client.sh        # macOS / Linux client (bash + curl)
└── server/
    ├── app.py                     # FastAPI 服务（API + 控制台）
    ├── README.md                  # server 端详细说明
    └── templates/
        └── dashboard.html         # Web 控制台页面
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

把 `scripts/telemetry_client.ps1`（必选，Windows 用户多）和/或 `scripts/telemetry_client.sh`（可选，Mac/Linux）复制到你的 skill 的 `scripts/` 目录下。

```
your-skill/
├── SKILL.md
└── scripts/
    ├── telemetry_client.ps1
    └── telemetry_client.sh   # 可选
```

### Step 2：在 SKILL.md 末尾插入引导文案

打开 `SKILL_片段.md`，把里面的内容复制粘贴到你的 `SKILL.md` 末尾，并把 `<在这里替换为本 skill 的名称>` 改成你 skill 的真实名字。

文案的核心是引导大模型在每次执行 skill 主流程完毕后**静默执行一次** client 脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/telemetry_client.ps1" -SkillName "your_skill_name"
```

或者（macOS / Linux）：

```bash
bash scripts/telemetry_client.sh "your_skill_name"
```

> **重要**：脚本被设计为**完全静默 + 永远 exit 0**，绝不会污染大模型上下文，也绝不会中断 skill 主流程。失败会被默默吞掉并把事件落到本地队列。

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
    client_version  TEXT
);
```

每次 skill 调用 = 一条明细记录，便于后续任意维度的聚合分析。

---

## 五、跨用户环境兼容性

Client 脚本经过特别设计，确保在任何用户机器上都能跑：

- **Windows**：兼容 PowerShell 5.1（Win10 默认）和 PowerShell 7+；用 `-ExecutionPolicy Bypass` 启动绕过本地策略；路径用 `$env:LOCALAPPDATA` 兼容中文用户名
- **macOS / Linux**：仅依赖 bash + curl；用户名 fallback 到 `id -un`；路径用 `$XDG_CACHE_HOME` / `$HOME/.cache`
- **网络**：3 秒超时，跳过系统代理，避免被防火墙/代理拖慢
- **离线**：失败事件落 `%LOCALAPPDATA%\SkillTelemetry\queue.jsonl`（Windows）或 `~/.cache/skill-telemetry/queue.jsonl`（Unix），下次调用时优先批量补传
- **并发**：用原子重命名 + 单行原子追加，避免多个 skill 并发写时损坏队列
- **静默**：所有错误吞掉，永远 `exit 0`，绝不打断 skill 主流程

---

## 六、常见问题

**Q：Server 离线了怎么办？会丢数据吗？**
A：不会。client 把失败事件写入本地 JSONL 队列，下次任意一个 skill 调用时会先尝试批量补传。

**Q：脚本执行会不会让用户看到弹窗或乱码？**
A：不会。脚本全程静默，无任何 stdout/stderr 输出，也不会弹任何窗口。

**Q：如何修改 server 地址？**
A：在用户机器上设置环境变量 `SKILL_TELEMETRY_URL` 即可，无需改 client 脚本。

**Q：要不要鉴权？**
A：当前版本假设公司内网信任环境，未做鉴权。如需添加，可在 server 加 token header 校验，client 端从环境变量读取并发送。

---

## License

Internal use within the company.
