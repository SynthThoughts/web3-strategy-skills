"""原子状态管理：tempfile + os.replace + flock。"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

from .config import state_dir


def state_path(name: str) -> Path:
    return state_dir() / f"{name}_state.json"


def load_state(name: str) -> dict:
    p = state_path(name)
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def save_state(name: str, data: dict) -> None:
    """原子写入状态文件。"""
    p = state_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- 进程锁 ---

_lock_fd = None


def acquire_lock(name: str) -> bool:
    """获取互斥锁，防止多实例并发。"""
    global _lock_fd
    lock_path = state_dir() / f".{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (OSError, IOError):
        _lock_fd.close()
        _lock_fd = None
        return False


def release_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None
