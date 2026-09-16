#!/usr/bin/env python3
"""
mako food sitemap local runner for Windows Task Scheduler (once a day).
Launch with pythonw.exe so no console window appears.

    pull -> crawl + build docs/mako/food/*.xml -> commit+push if changed

Runs next to the 5-minute `v1-feed poll` task: the build itself only touches
docs/mako/food, and the push retries after a rebase if the poll pushed in between.
Everything is logged to logs/food/run.log.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs" / "food"
LOG = LOG_DIR / "run.log"
FEED_BASE_URL = "https://zappler1992.github.io/v1-feed"
PYTHON = Path(sys.executable).with_name("python.exe")   # console python (we may run under pythonw.exe)
CREATE_NO_WINDOW = 0x08000000
OUT = "docs/mako/food"


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run(cmd, env=None, timeout=600):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=CREATE_NO_WINDOW, env=env, timeout=timeout)
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            log(line.rstrip())
    return p.returncode


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG_DIR / "run.old.log")
    log("=== run start")

    env = {**os.environ, "FEED_BASE_URL": FEED_BASE_URL, "PYTHONIOENCODING": "utf-8"}
    run(["git", "pull", "--rebase", "--quiet", "origin", "main"])

    # full crawl of ~24k urls: allow up to 2 hours
    if run([str(PYTHON), "scripts/build_food_sitemap.py", "build"], env, timeout=7200) != 0:
        log("build FAILED")
        return 1

    run(["git", "add", OUT])
    if run(["git", "diff", "--cached", "--quiet", "--", OUT]) == 0:
        log("no change")
        log("=== run end")
        return 0

    run(["git", "-c", "user.name=v1-feed-bot", "-c", "user.email=v1-feed-bot@users.noreply.github.com",
         "commit", "-q", "-m", "mako food sitemap update (local)"])
    for attempt in range(3):
        if run(["git", "push", "-q", "origin", "main"]) == 0:
            log("pushed")
            break
        log(f"push failed (attempt {attempt + 1}), rebasing")
        run(["git", "pull", "--rebase", "--quiet", "origin", "main"])
    else:
        log("push FAILED")
        return 1

    log("=== run end")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never leave the scheduler without a trace
        log(f"runner CRASHED: {e!r}")
        sys.exit(1)
