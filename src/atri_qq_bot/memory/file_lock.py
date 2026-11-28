"""Coordinate access to each JSON memory file within the running process."""

from __future__ import annotations

import threading
import weakref
from pathlib import Path

_registry_lock = threading.Lock()
_locks: weakref.WeakValueDictionary[Path, threading.RLock] = weakref.WeakValueDictionary()


def memory_file_lock(path: Path) -> threading.RLock:
    key = path.resolve()
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


def _locked_mutation(method):
    """为主消息写入与后台整合共用一把进程内可重入锁。"""
    def wrapped(self, *args, **kwargs):
        with self._memory_state_lock:
            return method(self, *args, **kwargs)
    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped
