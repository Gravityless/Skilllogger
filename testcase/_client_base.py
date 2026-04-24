"""
通用 client 测试基类。子类只需指定 client kind ('bash'/'ps') 与 queue 子目录名。
覆盖场景 C1-C7。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.server_fixture import TelemetryServer, _make_tmpdir
from common import client_runner


class _ClientTestMixin:
    CLIENT_KIND = "bash"  # 'bash' or 'ps'
    QUEUE_SUBDIR = "skill-telemetry"

    # ---- helpers ----
    def _make_queue_dir(self, parent: Path) -> Path:
        d = parent / self.QUEUE_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, skill, queue_dir, server_url, extra_env=None):
        if self.CLIENT_KIND == "bash":
            return client_runner.run_bash_client(skill, queue_dir, server_url, extra_env)
        return client_runner.run_ps_client(skill, queue_dir, server_url, extra_env)

    def _queue_file(self, qd: Path) -> Path:
        return qd / "queue.jsonl"

    # ---- C1 ----
    def test_C1_online_single_report(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            r = self._run("skill_C1", qd, srv.url)
            self.assertEqual(r.returncode, 0, msg=r.stderr.decode("utf-8", "replace"))
            self.assertEqual(srv.count_events(skill="skill_C1"), 1)

    # ---- C2 ----
    def test_C2_offline_enqueue(self):
        with _make_tmpdir() as tmp:
            qd = self._make_queue_dir(Path(tmp))
            # 指向一个肯定关闭的端口
            r = self._run("skill_C2", qd, "http://127.0.0.1:1")
            self.assertEqual(r.returncode, 0)
            qf = self._queue_file(qd)
            self.assertTrue(qf.exists(), "queue.jsonl should exist after offline call")
            lines = [l for l in qf.read_text("utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(obj["skill"], "skill_C2")

    # ---- C3 ----
    def test_C3_offline_then_recover(self):
        with _make_tmpdir() as tmp:
            qd = self._make_queue_dir(Path(tmp))
            # 第一次离线：落队列
            r1 = self._run("skill_C3", qd, "http://127.0.0.1:1")
            self.assertEqual(r1.returncode, 0)
            qf = self._queue_file(qd)
            self.assertTrue(qf.exists())

            # 第二次：server 起来
            with TelemetryServer() as srv:
                r2 = self._run("skill_C3", qd, srv.url)
                self.assertEqual(r2.returncode, 0)
                # 队列应被清空（或不存在 / 0 字节）
                if qf.exists():
                    self.assertEqual(qf.stat().st_size, 0,
                                     msg=f"queue not drained: {qf.read_text('utf-8')!r}")
                # server 应收到 2 条
                self.assertEqual(srv.count_events(skill="skill_C3"), 2)

    # ---- C4 ----
    def test_C4_orphan_recovered(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            # 造一个 5 分钟前的 sending 文件（视为孤儿）
            orphan = qd / "queue.sending.orphan_test.jsonl"
            event = {
                "username": "tester",
                "skill": "skill_C4_orphan",
                "hostname": "h",
                "timestamp": "2024-01-01T00:00:00.000Z",
                "client_version": "1.0.0",
            }
            orphan.write_text(json.dumps(event) + "\n", encoding="utf-8")
            old_ts = time.time() - 5 * 60
            os.utime(orphan, (old_ts, old_ts))

            r = self._run("skill_C4_now", qd, srv.url)
            self.assertEqual(r.returncode, 0)
            # 孤儿文件应被回收（删除）
            self.assertFalse(orphan.exists(), "orphan sending file should be recycled")
            # server 收到孤儿事件 + 当前事件
            self.assertEqual(srv.count_events(skill="skill_C4_orphan"), 1)
            self.assertEqual(srv.count_events(skill="skill_C4_now"), 1)

    # ---- C5 ----
    def test_C5_fresh_sending_kept(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            fresh = qd / "queue.sending.fresh_test.jsonl"
            event = {
                "username": "tester",
                "skill": "skill_C5_fresh",
                "hostname": "h",
                "timestamp": "2024-01-01T00:00:00.000Z",
                "client_version": "1.0.0",
            }
            fresh.write_text(json.dumps(event) + "\n", encoding="utf-8")
            # mtime 保持当前 → 不应被回收

            r = self._run("skill_C5_now", qd, srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertTrue(fresh.exists(), "fresh sending file must NOT be touched")
            # 当前事件正常上报
            self.assertEqual(srv.count_events(skill="skill_C5_now"), 1)
            # fresh 没被回收
            self.assertEqual(srv.count_events(skill="skill_C5_fresh"), 0)

    # ---- C6 ----
    def test_C6_missing_skill_silent(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            # 不传 skill_name
            r = self._run(None, qd, srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(srv.count_events(), 0)

    # ---- C7 ----
    def test_C7_corrupted_queue_line_skipped(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            qf = self._queue_file(qd)
            good = {
                "username": "tester",
                "skill": "skill_C7_good",
                "hostname": "h",
                "timestamp": "2024-01-01T00:00:00.000Z",
                "client_version": "1.0.0",
            }
            qf.write_text(
                "this is not json at all\n" + json.dumps(good) + "\n",
                encoding="utf-8",
            )

            r = self._run("skill_C7_now", qd, srv.url)
            self.assertEqual(r.returncode, 0)
            # 好行被上报；坏行被丢弃；当前事件被上报
            self.assertEqual(srv.count_events(skill="skill_C7_good"), 1)
            self.assertEqual(srv.count_events(skill="skill_C7_now"), 1)
