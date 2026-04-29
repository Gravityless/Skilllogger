#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 数据埋点 Python client (推荐首选; 跨平台; 零阻塞 fire-and-forget)
=====================================================================

用法:
    python3 scripts/telemetry_client.py <skill_name>

设计目标
--------
对调用方 (大模型 / skill 主流程) **真正零阻塞**:
父进程把事件原子追加到本地队列后, 立即 spawn 一个 detached 子进程接管所有 HTTP
I/O, 父进程在数十毫秒内退出, 不受 server 在线 / 网络状况影响.

与 telemetry_client.sh / .ps1 (旧方案) 行为对齐
----------------------------------------------
- 全程静默, 永不抛错给调用方
- 离线容错: server 不可达时事件落本地 JSONL 队列, 下次调用时优先批量补传
- 孤儿 sending 文件回收 (>60s) + rename 原子认领防多 client 重复消费
- event_id 客户端生成, 服务端 INSERT OR IGNORE 保证 exactly-once
- 短超时 (3s), 跳过系统代理
- 永远 exit 0

队列路径 (与旧方案完全兼容, 同一台机器多种 client 共享)
   - Unix : ${XDG_CACHE_HOME:-~/.cache}/skill-telemetry/queue.jsonl
   - Win  : %LOCALAPPDATA%/SkillTelemetry/queue.jsonl

server 地址: 默认 http://localhost:8000, 可通过环境变量 SKILL_TELEMETRY_URL 覆盖.

零依赖 (仅用 Python 标准库): 不依赖 requests / aiohttp 等第三方包,
保证用户机器上只要有 python3 就能跑.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

CLIENT_VERSION = "1.0.0-py"
TIMEOUT_SEC = 3
ORPHAN_AGE_SEC = 60
DEFAULT_SERVER_URL = "http://localhost:8000"
WORKER_FLAG = "--worker"

IS_WINDOWS = sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# 路径 / 环境
# ---------------------------------------------------------------------------
def _queue_dir() -> Path:
    """跨平台缓存目录, 与 sh / ps1 客户端保持一致, 实现三种 client 共享同一队列."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SkillTelemetry"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "skill-telemetry"


def _queue_file() -> Path:
    return _queue_dir() / "queue.jsonl"


def _username() -> str:
    for var in ("USER", "USERNAME"):
        v = os.environ.get(var)
        if v:
            return v
    try:
        import getpass
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown"
    except Exception:
        return "unknown"


def _now_iso_ms() -> str:
    # ISO8601 UTC, 毫秒精度 (与 ps1 的 'yyyy-MM-ddTHH:mm:ss.fffZ' 对齐)
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# 事件构造 / 落盘
# ---------------------------------------------------------------------------
def _build_event(skill_name: str) -> dict:
    return {
        "event_id": uuid.uuid4().hex,
        "username": _username(),
        "skill": skill_name,
        "hostname": _hostname(),
        "timestamp": _now_iso_ms(),
        "client_version": CLIENT_VERSION,
    }


def _atomic_append_line(path: Path, line: str) -> None:
    """单次 write(line + '\\n') 到 'ab' 模式打开的文件; 内容 < PIPE_BUF 时
    POSIX O_APPEND 保证多进程并发追加不会撕裂行 (Win 上类似的 atomic append 行为也成立).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    with open(path, "ab") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# Detached 子进程 spawn (fire-and-forget 的核心)
# ---------------------------------------------------------------------------
def _spawn_worker() -> None:
    """启动一个完全脱离父进程会话的 worker, 父进程立即返回.

    Unix : start_new_session=True (新 setsid) + 关闭三标准流 + close_fds
    Win  : DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    任何 spawn 失败都被吞掉 — 队列里的事件下次调用时仍会被消费.
    """
    try:
        cmd = [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG]
        kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if IS_WINDOWS:
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            kwargs["creationflags"] = (
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker: HTTP / 队列消费
# ---------------------------------------------------------------------------
def _post_json(url: str, body: bytes) -> bool:
    """POST 一段 JSON; 2xx 返回 True, 其它/异常返回 False. 显式跳过系统代理."""
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        # ProxyHandler({}) 显式禁用所有系统代理 (对齐 sh 的 --noproxy '*' 与 ps1 的 $req.Proxy=$null)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT_SEC) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception:
        return False


def _claim(src: Path, dst: Path) -> bool:
    """rename 原子认领; 返回是否抢到. 多 client 并发时只有一个能成功."""
    try:
        os.rename(str(src), str(dst))
        return True
    except OSError:
        return False


def _append_file_to_queue(src: Path, queue: Path) -> None:
    """把 src 的内容 append 回 queue (而非覆盖), 保护期间产生的新事件."""
    try:
        if not src.exists():
            return
        data = src.read_bytes()
        if data:
            if not data.endswith(b"\n"):
                data += b"\n"
            queue.parent.mkdir(parents=True, exist_ok=True)
            with open(queue, "ab") as f:
                f.write(data)
        try:
            src.unlink()
        except OSError:
            pass
    except Exception:
        pass


def _recycle_orphans(queue_dir: Path, queue: Path) -> None:
    """Step 0: 回收孤儿 sending 文件 (mtime > ORPHAN_AGE_SEC).

    用 rename 认领防止多 client 并发回收同一孤儿造成 queue 重复. 认领后仍保留
    queue.sending.* 命名 — 万一本实例又被杀, 下一轮还能再次回收.
    """
    try:
        now = time.time()
        for orphan in queue_dir.glob("queue.sending.*.jsonl"):
            try:
                mtime = orphan.stat().st_mtime
            except OSError:
                continue
            if (now - mtime) <= ORPHAN_AGE_SEC:
                continue
            claim_name = "queue.sending.recover.{}.{}.jsonl".format(
                os.getpid(), int(now)
            )
            claim = queue_dir / claim_name
            if _claim(orphan, claim):
                _append_file_to_queue(claim, queue)
    except Exception:
        pass


def _flush_queue(queue_dir: Path, queue: Path, server_url: str) -> None:
    """Step 1: 抢占 queue.jsonl → 解析 → POST /track/batch → 失败回滚."""
    try:
        if not queue.exists() or queue.stat().st_size == 0:
            return
    except OSError:
        return

    sending = queue_dir / "queue.sending.{}.{}.jsonl".format(os.getpid(), int(time.time()))
    if not _claim(queue, sending):
        return  # 被并发实例抢走

    try:
        events = []
        try:
            with open(sending, "rb") as f:
                for raw in f:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line or not (line.startswith("{") and line.endswith("}")):
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        # 损坏行直接丢弃 (与 sh / ps1 行为一致)
                        continue
        except OSError:
            return

        if not events:
            try:
                sending.unlink()
            except OSError:
                pass
            return

        body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
        if _post_json(server_url.rstrip("/") + "/track/batch", body):
            try:
                sending.unlink()
            except OSError:
                pass
        else:
            _append_file_to_queue(sending, queue)
    except Exception:
        # 任何意外都把 sending 还回主队列
        _append_file_to_queue(sending, queue)


def _worker_main() -> None:
    """子进程入口: detached 后唯一的存在意义就是把队列搬到 server."""
    try:
        server_url = os.environ.get("SKILL_TELEMETRY_URL") or DEFAULT_SERVER_URL
        qd = _queue_dir()
        qf = _queue_file()
        try:
            qd.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        _recycle_orphans(qd, qf)
        _flush_queue(qd, qf, server_url)
    except Exception:
        pass
    finally:
        # 测试桥: 仅在 SKILL_TELEMETRY_WORKER_DONE_FILE 被设置时, 写入一个标记文件,
        # 让测试代码能可靠等到 fire-and-forget 子进程结束再断言. 生产环境不设置该
        # 变量, 行为完全不变.
        marker = os.environ.get("SKILL_TELEMETRY_WORKER_DONE_FILE")
        if marker:
            try:
                Path(marker).parent.mkdir(parents=True, exist_ok=True)
                with open(marker, "ab") as f:
                    f.write(b"done\n")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Parent: 父进程入口 (目标 < 50ms 退出)
# ---------------------------------------------------------------------------
def _parent_main(skill_name: str) -> None:
    try:
        event = _build_event(skill_name)
        line = json.dumps(event, ensure_ascii=False)
        _atomic_append_line(_queue_file(), line)
    except Exception:
        # 落盘失败也不要拖累调用方; spawn 仍然尝试 (它会消费已有队列)
        pass
    _spawn_worker()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv) -> int:
    try:
        if len(argv) >= 2 and argv[1] == WORKER_FLAG:
            _worker_main()
            return 0

        # 父模式: 缺参数静默退出 (与 sh / ps1 行为一致, 兼容大模型偶尔漏传)
        if len(argv) < 2:
            return 0
        skill_name = (argv[1] or "").strip()
        if not skill_name:
            return 0
        _parent_main(skill_name)
    except Exception:
        # 永远不要把异常传出
        pass
    return 0


if __name__ == "__main__":
    # 永远 exit 0, 保证不打断 skill 主流程
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
