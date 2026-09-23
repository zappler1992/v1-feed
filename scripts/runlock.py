"""
A tiny cross-process lock, so the 5-minute full run and the 2-minute IndexNow run
never touch state/ or docs/ at the same time.

Whoever holds it works; whoever does not simply skips this tick - both tasks run
often enough that skipping one is harmless. A lock older than STALE_SECONDS is
assumed to belong to a crashed run and is taken over.
"""
import os
import time
from pathlib import Path

LOCK_FILE = Path(__file__).resolve().parent.parent / "logs" / ".run.lock"
STALE_SECONDS = 15 * 60


class Busy(Exception):
    """Another run holds the lock."""


class lock:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
                held_by = LOCK_FILE.read_text(encoding="utf-8").strip()
            except OSError:  # vanished between the two calls: treat as free, retry once
                return self.__enter__()
            if age < STALE_SECONDS:
                raise Busy(f"held by {held_by} for {age:.0f}s")
            LOCK_FILE.unlink(missing_ok=True)
            return self.__enter__()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{self.owner} pid={os.getpid()} at={time.strftime('%Y-%m-%d %H:%M:%S')}")
        return self

    def __exit__(self, *exc):
        LOCK_FILE.unlink(missing_ok=True)
        return False
