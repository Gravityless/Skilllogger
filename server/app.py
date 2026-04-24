"""
Skill Telemetry Server
======================
接收 client 上报的 skill 调用事件，落地到 SQLite，并提供查询 API 与 Web 控制台。

启动方式:
    pip install fastapi uvicorn jinja2
    uvicorn app:app --host 0.0.0.0 --port 8000

数据库文件默认位于本脚本同目录下的 telemetry.db，可通过环境变量 TELEMETRY_DB 覆盖。

数据库初始化策略 (启动时执行):
    1. 若设置环境变量 TELEMETRY_NEW_DB=1 (或 true/yes) → 强制新建:
       若旧库存在则重命名为 telemetry.db.bak.<时间戳> 备份, 再创建空库.
    2. 否则:
       - 库文件不存在 → 创建空库并建表.
       - 库文件已存在 → 直接复用 (CREATE TABLE IF NOT EXISTS 兼容).
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TELEMETRY_DB", BASE_DIR / "telemetry.db"))

app = FastAPI(title="Skill Telemetry Server", version="1.0.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def init_db() -> None:
    reset = _truthy(os.environ.get("TELEMETRY_NEW_DB"))
    db_existed = DB_PATH.exists()

    if reset and db_existed:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = DB_PATH.with_name(f"{DB_PATH.name}.bak.{ts}")
        # Windows 上前一个进程刚释放 SQLite 文件可能仍被内核短暂占用，重试几次
        last_err: Optional[BaseException] = None
        for attempt in range(10):
            try:
                DB_PATH.rename(backup)
                last_err = None
                break
            except OSError as exc:
                last_err = exc
                # 跨文件系统时 rename 失败：尝试 copy + unlink
                try:
                    shutil.copy2(DB_PATH, backup)
                    DB_PATH.unlink(missing_ok=True)
                    last_err = None
                    break
                except OSError as exc2:
                    last_err = exc2
                    time.sleep(0.2)
        if last_err is not None:
            # 实在备份不掉旧库就放弃 reset，保留旧库（绝不丢数据）
            print(f"[telemetry] WARN: cannot backup old db ({last_err}); keep using existing", flush=True)
        else:
            print(f"[telemetry] reset db, backup -> {backup}", flush=True)
            db_existed = False

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL,
                skill           TEXT NOT NULL,
                hostname        TEXT,
                client_ts       TEXT NOT NULL,
                server_ts       TEXT NOT NULL,
                client_version  TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user_skill ON events(username, skill)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_skill ON events(skill)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_client_ts ON events(client_ts)")
        conn.commit()

    if not db_existed:
        print(f"[telemetry] created new db: {DB_PATH}", flush=True)
    else:
        print(f"[telemetry] using existing db: {DB_PATH}", flush=True)


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def _on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------
class Event(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    skill: str = Field(..., min_length=1, max_length=256)
    hostname: Optional[str] = Field(None, max_length=128)
    timestamp: str = Field(..., description="客户端 ISO8601 时间戳")
    client_version: Optional[str] = Field(None, max_length=32)


class BatchEvents(BaseModel):
    events: List[Event]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_events(events: List[Event]) -> int:
    if not events:
        return 0
    server_ts = _now_iso()
    rows = [
        (e.username, e.skill, e.hostname, e.timestamp, server_ts, e.client_version)
        for e in events
    ]
    with db_conn() as conn:
        conn.executemany(
            "INSERT INTO events (username, skill, hostname, client_ts, server_ts, client_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 上报接口
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "time": _now_iso()}


@app.post("/track")
def track(event: Event):
    try:
        _insert_events([event])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "received": 1}


@app.post("/track/batch")
def track_batch(payload: BatchEvents):
    if not payload.events:
        return {"ok": True, "received": 0}
    try:
        n = _insert_events(payload.events)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "received": n}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------
GROUP_BY_MAP = {
    "user": ["username"],
    "skill": ["skill"],
    "user_skill": ["username", "skill"],
    "day": ["substr(client_ts, 1, 10)"],
}


def _build_query(
    username: Optional[str],
    skill: Optional[str],
    start: Optional[str],
    end: Optional[str],
    group_by: str,
):
    if group_by not in GROUP_BY_MAP:
        raise HTTPException(status_code=400, detail=f"invalid group_by: {group_by}")
    cols = GROUP_BY_MAP[group_by]
    select_cols = ", ".join(cols)
    alias = {
        "user": ["username"],
        "skill": ["skill"],
        "user_skill": ["username", "skill"],
        "day": ["day"],
    }[group_by]
    select_cols_aliased = ", ".join(f"{c} AS {a}" for c, a in zip(cols, alias))

    where = []
    params: list = []
    if username:
        where.append("username LIKE ?")
        params.append(f"%{username}%")
    if skill:
        where.append("skill LIKE ?")
        params.append(f"%{skill}%")
    if start:
        where.append("client_ts >= ?")
        params.append(start)
    if end:
        where.append("client_ts <= ?")
        params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = (
        f"SELECT {select_cols_aliased}, "
        f"COUNT(*) AS call_count, "
        f"MAX(client_ts) AS last_call "
        f"FROM events {where_sql} "
        f"GROUP BY {select_cols} "
        f"ORDER BY call_count DESC"
    )
    return sql, params, alias


def _kpis(username, skill, start, end):
    where = []
    params: list = []
    if username:
        where.append("username LIKE ?"); params.append(f"%{username}%")
    if skill:
        where.append("skill LIKE ?"); params.append(f"%{skill}%")
    if start:
        where.append("client_ts >= ?"); params.append(start)
    if end:
        where.append("client_ts <= ?"); params.append(end)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT COUNT(*) AS total_calls, "
        f"COUNT(DISTINCT username) AS unique_users, "
        f"COUNT(DISTINCT skill) AS unique_skills "
        f"FROM events {where_sql}"
    )
    with db_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return {
        "total_calls": row["total_calls"] or 0,
        "unique_users": row["unique_users"] or 0,
        "unique_skills": row["unique_skills"] or 0,
    }


@app.get("/stats/query")
def stats_query(
    username: Optional[str] = None,
    skill: Optional[str] = None,
    start: Optional[str] = Query(None, description="ISO8601 起始时间，包含"),
    end: Optional[str] = Query(None, description="ISO8601 结束时间，包含"),
    group_by: str = Query("user_skill"),
    format: str = Query("json"),
    limit: int = Query(1000, ge=1, le=100000),
):
    sql, params, alias = _build_query(username, skill, start, end, group_by)
    sql_with_limit = sql + " LIMIT ?"
    with db_conn() as conn:
        rows = conn.execute(sql_with_limit, params + [limit]).fetchall()

    data = []
    for r in rows:
        item = {a: r[a] for a in alias}
        item["call_count"] = r["call_count"]
        item["last_call"] = r["last_call"]
        data.append(item)

    if format == "csv":
        buf = io.StringIO()
        # UTF-8 BOM 让 Excel 正确识别中文
        buf.write("\ufeff")
        fieldnames = alias + ["call_count", "last_call"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(row)
        buf.seek(0)
        filename = f"telemetry_{group_by}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return JSONResponse({"rows": data, "kpis": _kpis(username, skill, start, end)})


# 兼容性快捷接口
@app.get("/stats/summary")
def stats_summary():
    return stats_query(group_by="user_skill")


@app.get("/stats/by_user")
def stats_by_user():
    return stats_query(group_by="user")


@app.get("/stats/by_skill")
def stats_by_skill():
    return stats_query(group_by="skill")


# ---------------------------------------------------------------------------
# Web 控制台
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")
