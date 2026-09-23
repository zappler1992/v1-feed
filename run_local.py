#!/usr/bin/env python3
"""
v1-feed local runner for Windows Task Scheduler (every 5 minutes).
Launch with pythonw.exe so no console window appears.

    pull -> build v1 feed/sitemaps + mako homepage feeds -> commit+push if changed
         -> ping WebSub hub for each feed that changed
    (build_mako_all.py also submits every newly seen mako URL to IndexNow)

Everything is logged to logs/run.log.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from runlock import Busy, lock  # noqa: E402

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "run.log"
FEED_BASE_URL = "https://zappler1992.github.io/v1-feed"
PYTHON = Path(sys.executable).with_name("python.exe")   # console python (we may run under pythonw.exe)
CREATE_NO_WINDOW = 0x08000000
FEED_FILES = ["docs/feed.xml", "docs/news-sitemap.xml", "docs/sitemap.xml", "docs/video-sitemap.xml"]
MAKO_FILES = ["docs/mako/feed.xml"]
MAKO_ALL_FILES = ["docs/mako/all.xml"]


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
    try:
        with lock("run_local"):
            return run_all()
    except Busy as e:
        log(f"skipped: {e}")
        return 0


def run_all():
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG_DIR / "run.old.log")
    log("=== run start")

    env = {**os.environ, "FEED_BASE_URL": FEED_BASE_URL, "PYTHONIOENCODING": "utf-8"}
    run(["git", "pull", "--rebase", "--quiet", "origin", "main"])

    if run([str(PYTHON), "scripts/build_feed.py", "build"], env) != 0:
        log("build FAILED")
        return 1
    if run([str(PYTHON), "scripts/build_mako_home.py", "build"], env) != 0:
        log("mako build FAILED (continuing with v1)")
    if run([str(PYTHON), "scripts/build_mako_all.py", "build"], env) != 0:
        log("mako-all build FAILED (continuing)")

    run(["git", "add", "docs", "state"])
    feed_changed = run(["git", "diff", "--cached", "--quiet", "--"] + FEED_FILES) != 0
    mako_changed = run(["git", "diff", "--cached", "--quiet", "--"] + MAKO_FILES) != 0
    mako_all_changed = run(["git", "diff", "--cached", "--quiet", "--"] + MAKO_ALL_FILES) != 0
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
    if mako_changed:
        run([str(PYTHON), "scripts/build_mako_home.py", "ping"], env)
    if mako_all_changed:
        run([str(PYTHON), "scripts/build_mako_all.py", "ping"], env)

    log("=== run end")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never leave the scheduler without a trace
        log(f"runner CRASHED: {e!r}")
        sys.exit(1)
