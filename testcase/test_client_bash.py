"""
bash client 测试套件 (C1-C8)。

跳过条件:
- 缺 ``bash`` 或 ``curl`` 时整套 skip（CI 上保险，本地极少见）。
- Windows 跑测时 skip：Windows 用户应使用 PS client，bash client 仅面向 Unix。
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

# 让 testcase/ 自身可被解析为顶层包父目录，避免相对导入问题
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client_base import _ClientTestMixin  # noqa: E402


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"),
                     "bash + curl required")
@unittest.skipIf(sys.platform.startswith("win"),
                 "bash client targets Unix; Windows users should use the PS client")
class BashClientTests(_ClientTestMixin, unittest.TestCase):
    CLIENT_KIND = "bash"
    QUEUE_SUBDIR = "skill-telemetry"  # bash client 默认 queue 目录: $XDG_CACHE_HOME/skill-telemetry


if __name__ == "__main__":
    unittest.main()
