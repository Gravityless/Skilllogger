"""
启停本地 telemetry server 的测试 fixture。

每个 fixture 启动一个独立的 uvicorn 子进程，使用：
  - 随机空闲端口
  - 临时 TELEMETRY_DB 路径（默认置于临时目录）
  - 可选 TELEMETRY_NEW_DB 标志

用法：
    with TelemetryServer() as srv:
        # srv.url -> http://127.0.0.1:<port>
        # srv.db_path -> Path
        ...
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "server"

IS_WINDOWS = sys.platform.startswith("win")


def _make_tmpdir(prefix: str = "telemetry_test_"):
    """跨版本/跨平台的 TemporaryDirectory；Windows 上忽略清理时的占用错误。"""
    kwargs = {"prefix": prefix}
    if sys.version_info >= (3, 10):
        kwargs["ignore_cleanup_errors"] = True
    return tempfile.TemporaryDirectory(**kwargs)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


class TelemetryServer:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        new_db: bool = False,
        extra_env: Optional[dict] = None,
    ):
        self._tmpdir = None
        if db_path is None:
            self._tmpdir = _make_tmpdir()
            db_path = Path(self._tmpdir.name) / "telemetry.db"
        self.db_path = Path(db_path)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._proc: Optional[subprocess.Popen] = None
        self._new_db = new_db
        self._extra_env = extra_env or {}

    def __enter__(self) -> "TelemetryServer":
        env = os.environ.copy()
        env["TELEMETRY_DB"] = str(self.db_path)
        if self._new_db:
            env["TELEMETRY_NEW_DB"] = "1"
        else:
            env.pop("TELEMETRY_NEW_DB", None)
        env.update(self._extra_env)
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(SERVER_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if not _wait_http(self.url + "/health"):
            self._dump_and_kill()
            raise RuntimeError("telemetry server failed to start")
        return self

    def _dump_and_kill(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._proc and self._proc.stdout:
            try:
                output = self._proc.stdout.read().decode("utf-8", errors="replace")
                if output:
                    sys.stderr.write("\n--- server output ---\n" + output + "\n---\n")
            except Exception:
                pass

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._proc and self._proc.stdout:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
        self._proc = None
        # Windows 内核可能短暂保留 SQLite 文件句柄，给点时间释放
        if IS_WINDOWS:
            time.sleep(0.5)

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    # -------- 便捷查询 --------
    def count_events(self, username: Optional[str] = None, skill: Optional[str] = None) -> int:
        # 直接读 sqlite，避免依赖 server 还活着
        if not self.db_path.exists():
            return 0
        sql = "SELECT COUNT(*) FROM events WHERE 1=1"
        args = []
        if username:
            sql += " AND username = ?"
            args.append(username)
        if skill:
            sql += " AND skill = ?"
            args.append(skill)
        with sqlite3.connect(self.db_path) as c:
            return c.execute(sql, args).fetchone()[0]

    def http_count(self) -> int:
        try:
            with urllib.request.urlopen(self.url + "/stats/summary", timeout=3) as r:
                import json as _json
                data = _json.loads(r.read())
                return sum(item.get("call_count", 0) for item in data)
        except Exception:
            return -1
