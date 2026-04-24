"""
跨平台调用 telemetry client 脚本。

bash client: 任何平台只要有 bash + curl 就能跑。
PS client:  优先 pwsh（PowerShell 7+，跨平台），否则 powershell（Windows 自带）。
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
    """
    bash client 的 queue 路径基于 XDG_CACHE_HOME/skill-telemetry。
    所以我们让 XDG_CACHE_HOME 指向 queue_dir 的父目录，并保证子目录名为 skill-telemetry。
    简化做法：让 queue_dir 自身就是 <某临时目录>/skill-telemetry，并把父目录传给 XDG_CACHE_HOME。
    """
    env = os.environ.copy()
    # queue_dir 必须以 skill-telemetry 结尾
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
    """
    PS client 的 queue 路径基于 LOCALAPPDATA\\SkillTelemetry。
    queue_dir 必须以 SkillTelemetry 结尾，并把父目录传给 LOCALAPPDATA。
    """
    ps = find_powershell()
    if ps is None:
        raise RuntimeError("powershell/pwsh not found")
    if queue_dir.name != "SkillTelemetry":
        raise ValueError("queue_dir for PS client must end with 'SkillTelemetry'")
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(queue_dir.parent)
    env["SKILL_TELEMETRY_URL"] = server_url
    # PS 默认 USERNAME；测试用例可覆盖
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
