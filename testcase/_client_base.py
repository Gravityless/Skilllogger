"""
通用 client 测试基类。

设计要点
--------
- bash client 与 pwsh client 的行为完全对称（相同的 4 步状态机），故所有场景都
  抽进本 mixin；子类只需指定 ``CLIENT_KIND`` ('bash'/'ps') 与 ``QUEUE_SUBDIR``
  （bash → ``skill-telemetry``；pwsh → ``SkillTelemetry``，对应各自客户端实现的
  默认缓存目录名）。
- 每个用例都用 ``_make_tmpdir`` + 隔离的 ``TelemetryServer`` 起一套全新环境
  （随机端口、临时 DB、临时 queue 目录），用例之间无任何共享状态，可并发执行。
- 客户端通过 ``client_runner`` 子进程调起；queue 目录通过覆盖 ``XDG_CACHE_HOME``
  / ``LOCALAPPDATA`` 环境变量重定向到测试临时目录，从而不污染宿主机。

覆盖场景: C1-C8 (详见 ``testcase/README.md``)。
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
    # 子类必须覆盖：决定调用哪个 client 二进制以及对应的 queue 子目录名
    CLIENT_KIND = "bash"  # 'bash' or 'ps'
    QUEUE_SUBDIR = "skill-telemetry"

    # ---- helpers ----
    def _make_queue_dir(self, parent: Path) -> Path:
        """在临时根下创建一个名字符合客户端预期的 queue 目录。

        bash client 期望 ``$XDG_CACHE_HOME/skill-telemetry``；
        pwsh client 期望 ``$LOCALAPPDATA/SkillTelemetry``。
        client_runner 据此把 parent 作为对应环境变量传入子进程。
        """
        d = parent / self.QUEUE_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, skill, queue_dir, server_url, extra_env=None):
        if self.CLIENT_KIND == "bash":
            return client_runner.run_bash_client(skill, queue_dir, server_url, extra_env)
        return client_runner.run_ps_client(skill, queue_dir, server_url, extra_env)

    def _queue_file(self, qd: Path) -> Path:
        return qd / "queue.jsonl"

    # ---- C1: 在线 → 单事件直接入库 ----
    def test_C1_online_single_report(self):
        """server 可达时 Step 2 应一次性 POST 成功，不落队列。"""
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            r = self._run("skill_C1", qd, srv.url)
            self.assertEqual(r.returncode, 0, msg=r.stderr.decode("utf-8", "replace"))
            self.assertEqual(srv.count_events(skill="skill_C1"), 1)

    # ---- C2: 离线 → 入本地队列，永不丢 ----
    def test_C2_offline_enqueue(self):
        """指向一个肯定打不通的端口 (1)；client 应把事件 append 到 queue.jsonl 后静默退出。"""
        with _make_tmpdir() as tmp:
            qd = self._make_queue_dir(Path(tmp))
            # 127.0.0.1:1 几乎必定 connection refused，模拟 server 完全不可达
            r = self._run("skill_C2", qd, "http://127.0.0.1:1")
            self.assertEqual(r.returncode, 0)
            qf = self._queue_file(qd)
            self.assertTrue(qf.exists(), "queue.jsonl should exist after offline call")
            lines = [l for l in qf.read_text("utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(obj["skill"], "skill_C2")

    # ---- C3: 离线积压 → server 恢复后下次调用整批补传 ----
    def test_C3_offline_then_recover(self):
        """验证 Step 1 (积压补传) + Step 2 (本次事件) 在同一次调用里完成。"""
        with _make_tmpdir() as tmp:
            qd = self._make_queue_dir(Path(tmp))
            # 第一次：server 不通 → 落队列
            r1 = self._run("skill_C3", qd, "http://127.0.0.1:1")
            self.assertEqual(r1.returncode, 0)
            qf = self._queue_file(qd)
            self.assertTrue(qf.exists())

            # 第二次：起 server，再次调用
            with TelemetryServer() as srv:
                r2 = self._run("skill_C3", qd, srv.url)
                self.assertEqual(r2.returncode, 0)
                # 队列必须被清空（文件不存在或大小为 0）
                if qf.exists():
                    self.assertEqual(qf.stat().st_size, 0,
                                     msg=f"queue not drained: {qf.read_text('utf-8')!r}")
                # server 应收到 2 条：积压的 + 本次的
                self.assertEqual(srv.count_events(skill="skill_C3"), 2)

    # ---- C4: 孤儿 sending 文件回收 (mtime > 60s) ----
    def test_C4_orphan_recovered(self):
        """模拟「上一进程被强杀，留下了 sending 文件」的场景。

        Step 0 应当：把 60s 前的 sending 文件原子认领，append 回 queue.jsonl，
        随即 Step 1 把它和当前事件一起上传给 server。
        """
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
            # 用 utime 把 mtime 拨回 5 分钟前，确保被认定为孤儿
            old_ts = time.time() - 5 * 60
            os.utime(orphan, (old_ts, old_ts))

            r = self._run("skill_C4_now", qd, srv.url)
            self.assertEqual(r.returncode, 0)
            # 孤儿文件应被回收（删除）
            self.assertFalse(orphan.exists(), "orphan sending file should be recycled")
            # server 收到孤儿事件 + 当前事件
            self.assertEqual(srv.count_events(skill="skill_C4_orphan"), 1)
            self.assertEqual(srv.count_events(skill="skill_C4_now"), 1)

    # ---- C5: 新生 sending 文件不被误回收 (mtime < 60s) ----
    def test_C5_fresh_sending_kept(self):
        """正在被另一个并发实例使用的 sending 文件 mtime 必然很新；Step 0 不应碰它。"""
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

    # ---- C6: 缺参数静默退出 ----
    def test_C6_missing_skill_silent(self):
        """大模型偶尔可能漏传 SkillName；客户端必须 exit 0 且不上报任何事件。"""
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            r = self._run(None, qd, srv.url)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(srv.count_events(), 0)

    # ---- C7: 队列损坏行容错 ----
    def test_C7_corrupted_queue_line_skipped(self):
        """队列里出现非 JSON 行（磁盘损坏 / 异常截断）时，
        必须跳过坏行而不影响其它好行 + 当前事件。"""
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

    # ---- C8: event_id 幂等去重（端到端）----
    # 模拟「server 已收但 client 在 rm sending 之前被强杀」：
    #   1) 手工放一个孤儿 sending 文件（带固定 event_id）→ 触发一次 client → server 入库 1 条
    #   2) 再放一个内容完全相同（同 event_id）的孤儿 → 再触发一次 client → 该事件被 server
    #      端按 event_id 幂等丢弃，**仍然只有 1 条**
    # 这条用例验证了 client 重传 + server INSERT OR IGNORE 的端到端 exactly-once 语义。
    def test_C8_dedup_by_event_id_on_orphan_replay(self):
        with _make_tmpdir() as tmp, TelemetryServer() as srv:
            qd = self._make_queue_dir(Path(tmp))
            event_id = "c8testevent00000000000000000001"
            event = {
                "event_id": event_id,
                "username": "tester",
                "skill": "skill_C8_dup",
                "hostname": "h",
                "timestamp": "2024-01-01T00:00:00.000Z",
                "client_version": "1.0.0",
            }

            # 第一次：造孤儿 → 触发 Step 0 → 上传
            orphan1 = qd / "queue.sending.dup1.jsonl"
            orphan1.write_text(json.dumps(event) + "\n", encoding="utf-8")
            old_ts = time.time() - 5 * 60
            os.utime(orphan1, (old_ts, old_ts))
            r1 = self._run("skill_C8_now", qd, srv.url)
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(srv.count_events(skill="skill_C8_dup"), 1)

            # 第二次：再造一个相同 event_id 的孤儿 → client 仍会上传，但 server 必须去重
            orphan2 = qd / "queue.sending.dup2.jsonl"
            orphan2.write_text(json.dumps(event) + "\n", encoding="utf-8")
            os.utime(orphan2, (old_ts, old_ts))
            r2 = self._run("skill_C8_now2", qd, srv.url)
            self.assertEqual(r2.returncode, 0)
            self.assertEqual(
                srv.count_events(skill="skill_C8_dup"),
                1,
                "server should dedup by event_id; same id must not insert twice",
            )
