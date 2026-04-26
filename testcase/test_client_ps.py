"""
PowerShell client 测试套件 (C1-C8)。

PS 客户端在三大平台都能跑：
- Windows 自带 ``powershell`` (Windows PowerShell 5.1)
- Linux / macOS / Windows 上手装的 ``pwsh`` (PowerShell 7+)
若两者都不存在则整个 TestCase 自动 skip。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client_base import _ClientTestMixin  # noqa: E402
from common.client_runner import find_powershell  # noqa: E402


@unittest.skipUnless(find_powershell(), "powershell/pwsh not found")
class PsClientTests(_ClientTestMixin, unittest.TestCase):
    CLIENT_KIND = "ps"
    QUEUE_SUBDIR = "SkillTelemetry"  # PS client 默认 queue 目录: $LOCALAPPDATA\SkillTelemetry


if __name__ == "__main__":
    unittest.main()
