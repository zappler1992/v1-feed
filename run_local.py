#!/usr/bin/env python3
"""
v1-feed local runner for Windows Task Scheduler (every 5 minutes).
Launch with pythonw.exe so no console window appears.

    pull -> build feed/sitemaps -> commit+push if changed -> ping WebSub hub if feed changed

Everything is logged to logs/run.log.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "run.log"
FEED_BASE_URL = "https://zappler1992.github.io/v1-feed"
PYTHON = Path(sys.executable).with_name("python.exe")   # console python (we may run under pythonw.exe)
CREATE_NO_WINDOW = 0x08000000
FEED_FILES = ["docs/feed.xml", "docs/news-sitemap.xml", "docs/sitemap.xml"]


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run(cmd, env=None, timeout=600):
    """Run a command with no window, log its output, return the exit code."""
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=CREATE_NO_WINDOW, env=env, timeout=timeout)
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            log(line.rstrip())
    return p.returncode


def main():
    LOG_DIR.mkdir(exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG_DIR / "run.old.log")
    log("=== run start")

    env = {**os.environ, "FEED_BASE_URL": FEED_BASE_URL, "PYTHONIOENCODING": "utf-8"}
    run(["git", "pull", "--rebase", "--quiet", "origin", "main"])

    if run([str(PYTHON), "scripts/build_feed.py", "build"], env) != 0:
        log("build FAILED")
        return 1

    run(["git", "add", "docs", "state"])
    feed_changed = run(["git", "diff", "--cached", "--quiet", "--"] + FEED_FILES) != 0
    any_changed = run(["git", "diff", "--cached", "--quiet"]) != 0

    if any_changed:
        run(["git", "-c", "user.name=v1-feed-bot", "-c", "user.email=v1-feed-bot@users.noreply.github.com",
             "commit", "-q", "-m", "feed update (local)"])
        if run(["git", "push", "-q", "origin", "main"]) != 0:
            log("push FAILED")
            return 1
        log("pushed")

    if feed_changed:
        run([str(PYTHON), "scripts/build_feed.py", "ping"], env)

    log("=== run end")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never leave the scheduler without a trace
        log(f"runner CRASHED: {e!r}")
        sys.exit(1)
