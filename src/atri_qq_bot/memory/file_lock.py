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
