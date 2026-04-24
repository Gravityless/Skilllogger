"""
PowerShell client 测试套件 (C1-C7)。
若机器上没有 pwsh / powershell，则整个 TestCase 被 skip。
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
    QUEUE_SUBDIR = "SkillTelemetry"


if __name__ == "__main__":
    unittest.main()
