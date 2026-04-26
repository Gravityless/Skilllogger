"""
server 端按 event_id 幂等去重测试 (S4/S5)。

设计目标: 在 client "传输完成但 sending 文件未删除"被强杀的极端场景下，下次启动重传
同一事件时，server 必须只落库一次 —— 即 *exactly-once* 语义。
实现: events.event_id TEXT NOT NULL UNIQUE + INSERT OR IGNORE。

| ID | 场景 |
| -- | ---- |
| S4 | 同一 event_id 调 /track 两次 → 只入库 1 行；第二次 inserted=0 |
| S5 | /track/batch 含 (重复 event_id 两条 + 一条新) → 入库 2 行；再发同 batch → inserted=0 |
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.server_fixture import TelemetryServer, _make_tmpdir  # noqa: E402


def _post(url: str, payload: dict) -> dict:
    """发送 JSON POST，绕过 client 直接验证 server 端去重。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _make_event(event_id: str, skill: str = "s_dedup") -> dict:
    """构造一条最小可入库的事件；event_id 必填。"""
    return {
        "username": "tester",
        "skill": skill,
        "hostname": "h",
        "timestamp": "2024-01-01T00:00:00.000Z",
        "client_version": "1.0.0",
        "event_id": event_id,
    }


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
        """batch 内去重 + 跨 batch 去重。"""
        with _make_tmpdir() as tmp:
            db = Path(tmp) / "telemetry.db"
            with TelemetryServer(db_path=db) as srv:
                eid_dup = "s5dupid000000000000000000000001"
                eid_new = "s5newid000000000000000000000002"
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

                # 再发同 batch → 全部命中已有 id
                r2 = _post(srv.url + "/track/batch", payload)
                self.assertEqual(r2.get("inserted"), 0)
                self.assertEqual(srv.count_events(skill="s5"), 2)


if __name__ == "__main__":
    unittest.main()
