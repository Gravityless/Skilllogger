"""
server 端按 event_id 幂等去重测试 (S4/S5/S6/S7)。

设计目标: 在 client 已"传输完成但 sending 文件未删除"被强杀的极端场景下，下次
启动重传同一事件时，server 端必须只落库一次 —— 这就是项目的 *exactly-once* 语义。
实现方式：events.event_id 列 + 部分唯一索引 (WHERE event_id IS NOT NULL) + INSERT OR IGNORE。

| ID | 场景 |
| -- | ---- |
| S4 | 同一 event_id 调 /track 两次 → 只入库 1 行；第二次 inserted=0 |
| S5 | /track/batch 含 (重复 event_id 两条 + 一条新) → 入库 2 行；再发同 batch → inserted=0 |
| S6 | 不传 event_id（兼容老 client）→ 不参与去重，每次都插入 |
| S7 | 老 schema (无 event_id 列) → 启动时自动 ALTER 补列，老数据保留，新去重生效 |

注意 SQLite 陷阱: ``INSERT ... ON CONFLICT(event_id) DO NOTHING`` 不能匹配
*部分*唯一索引；必须用 ``INSERT OR IGNORE``，行为等价但能命中部分索引。
"""
from __future__ import annotations

import json
import sys
import sqlite3
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.server_fixture import TelemetryServer, _make_tmpdir  # noqa: E402


def _post(url: str, payload: dict) -> dict:
    """发送 JSON POST，返回响应解析后的 dict。

    用 stdlib urllib 而非 client 脚本，目的是绕过 client，*直接*验证 server 的去重
    行为，避免被 client 端去重 / 重试逻辑混淆。
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _make_event(event_id: str | None, skill: str = "s_dedup") -> dict:
    """构造一条最小可入库的事件；event_id=None 用于 S6 兼容老 client 场景。"""
    e = {
        "username": "tester",
        "skill": skill,
        "hostname": "h",
        "timestamp": "2024-01-01T00:00:00.000Z",
        "client_version": "1.0.0",
    }
    if event_id is not None:
        e["event_id"] = event_id
    return e


class ServerDedupTests(unittest.TestCase):

    def test_S4_track_same_event_id_inserts_once(self):
        """单事件去重: 同 event_id 调 /track 两次，仅第一次 inserted=1。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            with TelemetryServer(db_path=db) as srv:
                ev = _make_event("s4eventid000000000000000000aaaa", skill="s4")
                r1 = _post(srv.url + "/track", ev)
                r2 = _post(srv.url + "/track", ev)
                self.assertEqual(r1.get("inserted"), 1)
                self.assertEqual(r2.get("inserted"), 0,
                                 f"second insert must be deduped, got {r2}")
                self.assertEqual(srv.count_events(skill="s4"), 1)

    def test_S5_batch_dedup_within_and_across(self):
        """batch 内去重 + 跨 batch 去重: 同 batch 内重复折叠，再发同 batch 全部命中。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            with TelemetryServer(db_path=db) as srv:
                eid_dup = "s5dupid000000000000000000000001"
                eid_new = "s5newid000000000000000000000002"
                # batch: 两条相同 + 一条新 → batch 内自身就有重复
                payload = {
                    "events": [
                        _make_event(eid_dup, skill="s5"),
                        _make_event(eid_dup, skill="s5"),
                        _make_event(eid_new, skill="s5"),
                    ]
                }
                r = _post(srv.url + "/track/batch", payload)
                self.assertEqual(r.get("received"), 3)
                self.assertEqual(r.get("inserted"), 2,
                                 f"in-batch dup must collapse, got {r}")
                self.assertEqual(srv.count_events(skill="s5"), 2)

                # 再发一次相同 batch → 全部命中已有 id，inserted=0（跨次去重）
                r2 = _post(srv.url + "/track/batch", payload)
                self.assertEqual(r2.get("inserted"), 0)
                self.assertEqual(srv.count_events(skill="s5"), 2)

    def test_S6_legacy_no_event_id_not_deduped(self):
        """老 client 不带 event_id → 部分唯一索引不约束 → 每次都入库（向后兼容）。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            with TelemetryServer(db_path=db) as srv:
                ev = _make_event(None, skill="s6")
                r1 = _post(srv.url + "/track", ev)
                r2 = _post(srv.url + "/track", ev)
                # event_id 为 NULL 时部分唯一索引不生效，二次插入也成功
                self.assertEqual(r1.get("inserted"), 1)
                self.assertEqual(r2.get("inserted"), 1)
                self.assertEqual(srv.count_events(skill="s6"), 2)

    def test_S7_schema_migration_from_old_db(self):
        """schema 迁移: 老库（无 event_id 列）启动后被 ALTER 补列，老数据保留 + 新去重生效。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            # 模拟 v1 时期的老 schema（没有 event_id 列）
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, "
                    "skill TEXT NOT NULL, hostname TEXT, client_ts TEXT NOT NULL, "
                    "server_ts TEXT NOT NULL, client_version TEXT)"
                )
                conn.execute(
                    "INSERT INTO events (username, skill, hostname, client_ts, server_ts, client_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy", "s7_legacy", "h",
                     "2023-01-01T00:00:00Z", "2023-01-01T00:00:00Z", "0.0.0"),
                )
                conn.commit()
            finally:
                conn.close()  # Windows 上不显式 close 会持有句柄

            with TelemetryServer(db_path=db) as srv:
                # 启动期 init_db 检测到缺列，自动 ALTER + 建部分唯一索引；老数据保留
                self.assertEqual(srv.count_events(skill="s7_legacy"), 1)
                # 新 client 走带 event_id 的去重路径
                ev = _make_event("s7newid00000000000000000000001a", skill="s7_new")
                _post(srv.url + "/track", ev)
                r2 = _post(srv.url + "/track", ev)
                self.assertEqual(r2.get("inserted"), 0)
                self.assertEqual(srv.count_events(skill="s7_new"), 1)


if __name__ == "__main__":
    unittest.main()
