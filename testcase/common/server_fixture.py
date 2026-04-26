"""
启停本地 telemetry server 的测试 fixture。

每个 fixture 启动一个**完全独立**的 uvicorn 子进程，使用：
  - 随机空闲端口（避免与系统服务/其他用例冲突，可并发）
  - 临时 ``TELEMETRY_DB`` 路径（每用例独立 DB，互不污染）
  - 可选 ``TELEMETRY_NEW_DB`` 标志（用于 S3 强制新建库）

设计要点：
  - server 以**子进程**方式真实启动 uvicorn，而不是 import app 直接调用，确保
    @app.on_event("startup") 钩子（即 init_db）真的被触发。
  - Windows 上 SQLite 文件句柄释放有延迟，stop() 后 sleep 0.5s 再让外层清理 tmpdir。
  - tempfile.TemporaryDirectory(ignore_cleanup_errors=...) 是 3.10+ 新增；
    为 3.8/3.9 兼容做了版本判断。

用法::

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
    """跨版本/跨平台的 TemporaryDirectory；Windows 上忽略清理时的占用错误。

    Python 3.10 引入 ``ignore_cleanup_errors=True``，能容忍清理时被占用的文件
    （Windows 上 SQLite/子进程未及时释放句柄时常见）。3.8/3.9 不支持该参数。
    """
    kwargs = {"prefix": prefix}
    if sys.version_info >= (3, 10):
        kwargs["ignore_cleanup_errors"] = True
    return tempfile.TemporaryDirectory(**kwargs)


def _free_port() -> int:
    """问内核要一个空闲端口；用 bind(0) 让 OS 分配，然后立即关闭。

    并发跑多个用例时此法天然防冲突。注意端口会在短暂的 TIME_WAIT 窗口里"被占用"
    但 SO_REUSEADDR 默认下 uvicorn bind 不会失败。
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    """轮询 /health，等 server 真正可服务后再返回，避免后续请求 ECONNREFUSED。"""
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
    """以子进程方式启停一个隔离的 telemetry server。

    通过 ``TELEMETRY_DB`` / ``TELEMETRY_NEW_DB`` 环境变量将 server 完全隔离到
    临时目录与随机端口。__exit__ 会优雅终止子进程并清理 tmpdir。
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        new_db: bool = False,
        extra_env: Optional[dict] = None,
    ):
        self._tmpdir = None
        if db_path is None:
            # 调用方未指定 db 路径 → 我们自己造一个临时目录 + 临时 db 文件
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
            # 防止外层 shell 已经 export 了该变量污染本用例
            env.pop("TELEMETRY_NEW_DB", None)
        env.update(self._extra_env)
        # cwd=SERVER_DIR 让 uvicorn 能用 "app:app" 的形式定位到 server/app.py
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
            # 启动失败：把 server stdout dump 出来便于排查
            self._dump_and_kill()
            raise RuntimeError("telemetry server failed to start")
        return self

    def _dump_and_kill(self):
        """启动失败时调用：dump 出 server stdout 后强杀。"""
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
        """优雅终止 + Windows 句柄释放延迟。"""
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
        # Windows 内核可能短暂保留 SQLite 文件句柄，给点时间释放，否则后续
        # tmpdir.cleanup() 会因 db 文件被占用而失败。
        if IS_WINDOWS:
            time.sleep(0.5)

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    # -------- 便捷查询 --------
    def count_events(self, username: Optional[str] = None, skill: Optional[str] = None) -> int:
        """直接读 sqlite 统计 events 行数；不依赖 server 进程是否还活着。

        所以即使 server 已 stop，依然能验证最终落库结果。
        """
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
        """走 HTTP /stats/summary 求总调用次数（用于 server 仍活着时的快速校验）。"""
        try:
            with urllib.request.urlopen(self.url + "/stats/summary", timeout=3) as r:
                import json as _json
                data = _json.loads(r.read())
                return sum(item.get("call_count", 0) for item in data)
        except Exception:
            return -1
