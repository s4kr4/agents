"""Reentrant process and thread exclusion shared by CLI and direct store calls."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator

from store_paths import checked_path


def lock_directory() -> Path:
    """Keep lock inodes in a fixed machine-local cache, never in a synced Vault."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    if not base.is_absolute():
        raise ValueError("lock cache directory must be absolute")
    directory = base / "llm-memory" / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory.resolve()


_registry_guard = threading.Lock()
_registry: dict[tuple[int, str], tuple[Any, Any]] = {}


@contextmanager
def store_lock(root: Path, timeout: float = 30.0) -> Iterator[None]:
    """Lock one resolved store; OS ownership is released even on process exit."""
    root = root.resolve()
    key = (os.getpid(), os.path.normcase(str(root)))
    with _registry_guard:
        mutex, state = _registry.setdefault(key, (threading.RLock(), threading.local()))
    deadline = time.monotonic() + timeout
    if not mutex.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"store lock timed out: {root}")
    try:
        if getattr(state, "depth", 0):
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return
        directory = lock_directory()
        digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
        path = checked_path(directory, directory / f"{digest}.lock")
        with path.open("a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
            else:
                import fcntl
            while True:
                try:
                    if sys.platform == "win32":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"store lock timed out: {root}") from exc
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            state.depth = 1
            try:
                yield
            finally:
                state.depth = 0
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        mutex.release()


def locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Protect the complete read-modify-write method, including nested calls."""

    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self.transaction():
            return method(self, *args, **kwargs)

    return wrapped
