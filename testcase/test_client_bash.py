"""
bash client 测试套件 (C1-C7)。
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
class BashClientTests(_ClientTestMixin, unittest.TestCase):
    CLIENT_KIND = "bash"
    QUEUE_SUBDIR = "skill-telemetry"


if __name__ == "__main__":
    unittest.main()
