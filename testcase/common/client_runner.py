"""
跨平台调用 telemetry client 脚本。

bash client (telemetry_client.sh): 任何平台只要有 bash + curl 就能跑。
PS   client (telemetry_client.ps1): 优先 pwsh（PowerShell 7+，跨平台），
                                    否则 powershell（Windows 自带）。

测试隔离的关键技巧——**环境变量重定向 queue 目录**：
  - bash client 使用 ``$XDG_CACHE_HOME/skill-telemetry`` 作为 queue 路径。
  - PS   client 使用 ``$LOCALAPPDATA/SkillTelemetry`` 作为 queue 路径。

  把这两个变量指向**测试用例独占的临时目录**，就能让每个用例的 queue 完全隔离，
  不会污染开发者本机的真实缓存目录，也不会让并发用例互相干扰。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
PS_CLIENT = REPO_ROOT / "scripts" / "telemetry_client.ps1"
BASH_CLIENT = REPO_ROOT / "scripts" / "telemetry_client.sh"


def find_powershell() -> Optional[str]:
    """查找可用的 PowerShell 解释器；优先 pwsh（7+ 跨平台），fallback 到 Windows 自带 powershell。"""
    for cand in ("pwsh", "powershell"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def run_bash_client(
    skill_name: Optional[str],
    queue_dir: Path,
    server_url: str,
    extra_env: Optional[dict] = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """跑一次 bash client 脚本。

    bash client 的 queue 路径硬编码为 ``$XDG_CACHE_HOME/skill-telemetry``。
    所以测试需要让 ``XDG_CACHE_HOME`` 指向 ``queue_dir`` 的**父目录**，并保证
    queue_dir 的目录名恰好是 ``skill-telemetry``。这样 client 内部拼出来的路径
    就是我们指定的 queue_dir，从而被测试完全控制。

    参数:
      skill_name: 传给 client 的 skill 名；None 表示故意不传（用于 C6）
      queue_dir : 必须以 'skill-telemetry' 结尾
      server_url: 通过 SKILL_TELEMETRY_URL 环境变量传给 client
      extra_env : 追加/覆盖环境变量
      timeout   : 子进程超时
    """
    env = os.environ.copy()
    if queue_dir.name != "skill-telemetry":
        raise ValueError("queue_dir for bash client must end with 'skill-telemetry'")
    env["XDG_CACHE_HOME"] = str(queue_dir.parent)
    env["SKILL_TELEMETRY_URL"] = server_url
    if extra_env:
        env.update(extra_env)
    args = ["bash", str(BASH_CLIENT)]
    if skill_name is not None:
        args.append(skill_name)
    return subprocess.run(
        args,
        env=env,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_ps_client(
    skill_name: Optional[str],
    queue_dir: Path,
    server_url: str,
    extra_env: Optional[dict] = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """跑一次 PowerShell client 脚本。

    PS client 的 queue 路径硬编码为 ``$LOCALAPPDATA\\SkillTelemetry``。同 bash 思路：
    让 ``LOCALAPPDATA`` 指向 ``queue_dir`` 的父目录，且 queue_dir 名为 ``SkillTelemetry``。

    USERNAME 也一并设置（PS client 用 ``%USERNAME%``；非 Windows 上默认为空，
    所以从 ``$USER`` 派生一个值，避免 client 报"username 为空"）。
    """
    ps = find_powershell()
    if ps is None:
        raise RuntimeError("powershell/pwsh not found")
    if queue_dir.name != "SkillTelemetry":
        raise ValueError("queue_dir for PS client must end with 'SkillTelemetry'")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(queue_dir.parent)
    env["SKILL_TELEMETRY_URL"] = server_url
    env.setdefault("USERNAME", env.get("USER", "tester"))
    if extra_env:
        env.update(extra_env)
    args = [
        ps,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PS_CLIENT),
    ]
    if skill_name is not None:
        args.extend(["-SkillName", skill_name])
    return subprocess.run(
        args,
        env=env,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
