"""
Python client 测试套件 (P1-P8 复用 C1-C8) + P9 零阻塞耗时验证.

跳过条件:
- python3 在所有 CI runner 与开发机上都默认存在, 无需额外 skip.

设计要点
--------
- Python client 是 fire-and-forget: 父进程把事件落盘后立即 spawn 一个 detached
  worker 子进程做所有 HTTP I/O. 父进程在数十毫秒内退出.
- 测试通过 ``SKILL_TELEMETRY_WORKER_DONE_FILE`` 环境变量让 worker 在结束时写
  一个标记文件, ``run_python_client`` 默认会等到该文件出现再返回, 让既有的
  C1-C8 断言可原样复用 (见 ``_client_base.py`` 的 _ClientTestMixin).
- P9 零阻塞专用测试不等 worker, 直接测父进程的 wallclock.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client_base import _ClientTestMixin  # noqa: E402
from common import client_runner  # noqa: E402
from common.server_fixture import _make_tmpdir  # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")
PY_QUEUE_SUBDIR = "SkillTelemetry" if IS_WINDOWS else "skill-telemetry"


class PythonClientTests(_ClientTestMixin, unittest.TestCase):
    """复用 C1-C8 八个通用场景: 一次编写, 三端 (bash/PS/python) 复用."""

    CLIENT_KIND = "python"
    QUEUE_SUBDIR = PY_QUEUE_SUBDIR


class PythonClientZeroBlockTests(unittest.TestCase):
    """P9: 验证 Python client 对调用方真正零阻塞.

    指向一个肯定打不通的 server (127.0.0.1:1, 通常 connection refused; 万一
    被某些防火墙黑洞还要超时), 测父进程从启动到退出的 wallclock. 由于父进程
    根本不做 HTTP I/O, 应当远小于 client 内部的 3s 超时和旧 sh/ps1 的 ~6s
    最坏阻塞.
    """

    # 给 CI 留够余量; 本地实测通常 < 200ms.
    WALLCLOCK_THRESHOLD_SEC = 1.5

    def test_P9_parent_returns_immediately_when_server_offline(self):
        with _make_tmpdir() as tmp:
            queue_dir = Path(tmp) / PY_QUEUE_SUBDIR
            queue_dir.mkdir(parents=True, exist_ok=True)

            t0 = time.monotonic()
            # wait_for_worker=False: 我们要测父进程, 不能被 worker 拖累
            r = client_runner.run_python_client(
                "skill_P9_zero_block",
                queue_dir,
                "http://127.0.0.1:1",
                wait_for_worker=False,
            )
            elapsed = time.monotonic() - t0

            self.assertEqual(r.returncode, 0,
                             msg=r.stderr.decode("utf-8", "replace"))
            self.assertLess(
                elapsed,
                self.WALLCLOCK_THRESHOLD_SEC,
                msg=(f"Python client parent wallclock {elapsed:.3f}s exceeded "
                     f"{self.WALLCLOCK_THRESHOLD_SEC}s — fire-and-forget broken?"),
            )


if __name__ == "__main__":
    unittest.main()
