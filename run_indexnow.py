#!/usr/bin/env python3
"""
Fast IndexNow runner for Windows Task Scheduler (every 2 minutes).
Launch with pythonw.exe so no console window appears.

Submitting to IndexNow does not depend on GitHub Pages, so this pass skips the feed
files and git entirely: fetch the sources -> record new URLs -> check them -> submit.
The 5-minute full run (run_local.py) still builds and publishes the feeds, and commits
the state file this pass updates. Both take the same lock, so they never overlap.

Logged to logs/indexnow.log.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from runlock import Busy, lock  # noqa: E402

LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "indexnow.log"
FEED_BASE_URL = "https://zappler1992.github.io/v1-feed"
PYTHON = Path(sys.executable).with_name("python.exe")
CREATE_NO_WINDOW = 0x08000000


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def main():
    LOG_DIR.mkdir(exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG_DIR / "indexnow.old.log")
    try:
        with lock("run_indexnow"):
            env = {**os.environ, "FEED_BASE_URL": FEED_BASE_URL, "PYTHONIOENCODING": "utf-8"}
            p = subprocess.run([str(PYTHON), "scripts/build_mako_all.py", "submit"],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=CREATE_NO_WINDOW, env=env, timeout=300)
            for line in (p.stdout + p.stderr).splitlines():
                if line.strip():
                    log(line.rstrip())
            if p.returncode != 0:
                log(f"submit FAILED (exit {p.returncode})")
            return p.returncode
    except Busy as e:
        log(f"skipped: {e}")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"runner CRASHED: {e!r}")
        sys.exit(1)
