"""
server 启动期 DB 初始化测试 (S1/S2/S3)。

S1: 无 db 文件 → 启动后 db 被创建, events 表存在
S2: 有 db 文件且含 1 条数据 → 默认启动 → 数据保留
S3: 有 db 文件且含 1 条数据 → TELEMETRY_NEW_DB=1 → 数据清空 + 旧库被备份

每个用例都通过 ``TelemetryServer`` 在子进程里以独立的 ``TELEMETRY_DB`` /
``TELEMETRY_NEW_DB`` 启动一份真实的 uvicorn server，端到端验证 ``init_db()``
的三态行为，而不是直接 import 函数 mock —— 这样能同时验证 FastAPI 的
``@app.on_event("startup")`` 钩子也被正确触发。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.server_fixture import TelemetryServer, _make_tmpdir  # noqa: E402


def _seed_db(db_path: Path):
    """造一个老结构的 db 并写入 1 条数据，用于 S2/S3。

    注意: ``with sqlite3.connect(...)`` 的上下文只 commit/rollback, **不会关闭连接**。
    Windows 上未关闭的连接会持有文件句柄，导致后续 server 进程内的 rename / unlink 失败。
    所以此处显式 ``conn.close()`` + ``gc.collect()`` 强制释放。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
            "skill TEXT NOT NULL, hostname TEXT, client_ts TEXT NOT NULL, "
            "server_ts TEXT NOT NULL, client_version TEXT)"
        )
        conn.execute(
            "INSERT INTO events (username, skill, hostname, client_ts, server_ts, client_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("seed_user", "seed_skill", "h", "2024-01-01T00:00:00Z",
             "2024-01-01T00:00:00Z", "0.0.0"),
        )
        conn.commit()
    finally:
        conn.close()
    import gc
    gc.collect()


class ServerDbInitTests(unittest.TestCase):

    def test_S1_no_db_creates_new(self):
        """无 db 文件 → 启动后自动创建 + 建表 + 0 条记录。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            self.assertFalse(db.exists())
            with TelemetryServer(db_path=db) as srv:
                self.assertTrue(srv.db_path.exists(),
                                "db file should be created on startup")
                # events 表存在
                conn = sqlite3.connect(srv.db_path)
                try:
                    rows = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
                    ).fetchall()
                finally:
                    conn.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(srv.count_events(), 0)

    def test_S2_existing_db_preserved(self):
        """已有 db + 1 条数据 + 默认启动 → 数据保留。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            _seed_db(db)
            self.assertEqual(_count(db), 1)
            with TelemetryServer(db_path=db) as srv:
                self.assertEqual(srv.count_events(), 1,
                                 "existing data must be preserved on default start")

    def test_S3_force_new_db_backups_old(self):
        """已有 db + 1 条 + TELEMETRY_NEW_DB=1 → 旧库被备份为 telemetry.db.bak.<ts>，新库为空。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            _seed_db(db)
            self.assertEqual(_count(db), 1)
            with TelemetryServer(db_path=db, new_db=True) as srv:
                # 新库应为空
                self.assertEqual(srv.count_events(), 0)
                # 至少存在一个备份文件
                backups = list(Path(tmp).glob("telemetry.db.bak.*"))
                self.assertGreaterEqual(len(backups), 1, "backup file expected")
                # 备份文件中应保留原数据
                self.assertEqual(_count(backups[0]), 1)


def _count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
