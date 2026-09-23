#!/usr/bin/env python3
"""
mako homepage feed: a snapshot RSS of what is on the mako.co.il homepage right now,
limited to the main item and the slider under it (the `mainComponent`), excluding the
sponsored teaser. The feed is regenerated every run and pinged to the WebSub hub when
its content changes.

Source: https://www.mako.co.il/?platform=mobileApp  (same mainComponent as the desktop site)
Output: docs/mako/feed.xml  (+ docs/mako/build.txt used to confirm GitHub Pages deployed)

Usage:
  python scripts/build_mako_home.py build
  python scripts/build_mako_home.py ping
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_feed import (FEED_BASE_URL, HUB_URL, USER_AGENT, esc, http_get, log,  # noqa: E402
                        normalize_url, rfc822, text_of, write_if_changed)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "mako"
SOURCE_URL = os.environ.get("MAKO_SOURCE_URL", "https://www.mako.co.il/?platform=mobileApp")
SITE_HOST = "www.mako.co.il"
FEED_TITLE = os.environ.get("MAKO_FEED_TITLE", "mako - הכתבה הראשית והסליידר בדף הבית")
FEED_DESCRIPTION = os.environ.get("MAKO_FEED_DESCRIPTION", "הכתבות המוצגות כרגע בראש דף הבית של mako.co.il (ללא תוכן ממומן)")
IL_TZ = ZoneInfo("Asia/Jerusalem")
PAGES_WAIT_SECONDS = int(os.environ.get("PAGES_WAIT_SECONDS", "240"))
ALLOWED_TYPES = {"mainItem", "regularTeaser"}


def feed_url():
    return f"{FEED_BASE_URL}/mako/feed.xml"


def parse_local_dt(s):
    """'2026-09-15T20:22' (Israel local time) -> epoch ms, or None."""
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=IL_TZ).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def is_sponsored(it):
    url = ((it.get("itemUrl") or {}).get("url") or "") if isinstance(it.get("itemUrl"), dict) else ""
    click = (it.get("itemUrl") or {}).get("domoClick") or {}
    return (it.get("itemType") not in ALLOWED_TYPES
            or bool(it.get("advertiserName"))
            or click.get("content_type") == "fabricated"
            or urlsplit(url).netloc.lower() != SITE_HOST)


def not_indexable():
    """URLs that build_mako_all.py already judged noindex / canonical-elsewhere."""
    f = ROOT / "state" / "mako_seen.json"
    if not f.exists():
        return set()
    try:
        pages = json.loads(f.read_text(encoding="utf-8")).get("pages", {})
    except json.JSONDecodeError:
        return set()
    return {u for u, p in pages.items() if p.get("indexable") is False}


def extract(data):
    """Items of the mainComponent, in on-page order, minus sponsored/ads and noindex pages."""
    comps = [c for c in data.get("components", []) if c.get("componentType") == "mainComponent"]
    if not comps:
        raise ValueError("mainComponent not found in source")
    blocked = not_indexable()
    items, skipped = [], []
    for it in comps[0].get("items", []):
        title = text_of(it.get("title"))
        if is_sponsored(it):
            skipped.append(f"{it.get('itemType')}: {title[:50]}")
            continue
        url = normalize_url(it["itemUrl"]["url"])
        if url in blocked:
            skipped.append(f"noindex: {title[:50]}")
            continue
        pics = it.get("pics") or []
        items.append({
            "url": url,
            "title": title,
            "description": text_of(it.get("subTitle")) or text_of(it.get("subtitle")),
            "author": text_of(it.get("author")),
            "flach": text_of(it.get("flach")),
            "image": pics[0].get("url") if pics and isinstance(pics[0], dict) else "",
            "published_ms": parse_local_dt((it.get("date") or {}).get("datetime")) if isinstance(it.get("date"), dict) else None,
            "position": "main" if it.get("itemType") == "mainItem" else "slider",
        })
    return items, skipped


def build_rss(items):
    dated = [i for i in items if i["published_ms"]]
    last_build = max((i["published_ms"] for i in dated), default=int(time.time() * 1000))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
           'xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:content="http://purl.org/rss/1.0/modules/content/">',
           '<channel>',
           f'<title>{esc(FEED_TITLE)}</title>',
           f'<link>https://{SITE_HOST}/</link>',
           f'<description>{esc(FEED_DESCRIPTION)}</description>',
           '<language>he</language>',
           f'<lastBuildDate>{rfc822(last_build)}</lastBuildDate>',
           f'<atom:link rel="hub" href="{esc(HUB_URL)}"/>',
           f'<atom:link rel="self" href="{esc(feed_url())}" type="application/rss+xml"/>']
    for i in items:
        out.append('<item>')
        out.append(f'<title>{esc(i["title"] or i["url"])}</title>')
        out.append(f'<link>{esc(i["url"])}</link>')
        out.append(f'<guid isPermaLink="true">{esc(i["url"])}</guid>')
        if i["published_ms"]:
            out.append(f'<pubDate>{rfc822(i["published_ms"])}</pubDate>')
        desc = i["description"] or i["title"]
        out.append(f'<description>{esc(desc)}</description>')
        out.append(f'<category>{"ראשי" if i["position"] == "main" else "סליידר"}</category>')
        if i["flach"]:
            out.append(f'<category>{esc(i["flach"])}</category>')
        if i["author"]:
            out.append(f'<dc:creator>{esc(i["author"])}</dc:creator>')
        body = (f'<p><a href="{esc(i["url"])}"><img src="{esc(i["image"])}" alt="{esc(i["title"])}"/></a></p>' if i["image"] else "")
        body += f'<p>{esc(desc)}</p>'
        out.append(f'<content:encoded><![CDATA[{body}]]></content:encoded>')
        if i["image"]:
            out.append(f'<media:thumbnail url="{esc(i["image"])}"/>')
            out.append(f'<media:content url="{esc(i["image"])}" medium="image"/>')
        out.append('</item>')
    out += ['</channel>', '</rss>', '']
    return "\n".join(out)


def cmd_build():
    now_ms = int(time.time() * 1000)
    data = None
    for attempt in (1, 2, 3):
        try:
            _, body = http_get(SOURCE_URL)
            data = json.loads(body.decode("utf-8"))
            break
        except (json.JSONDecodeError, UnicodeDecodeError, HTTPError, URLError, TimeoutError) as e:
            log(f"mako: source fetch/parse failed (attempt {attempt}/3): {type(e).__name__}: {str(e)[:120]}")
            time.sleep(15)
    if data is None:
        log("mako: source unavailable this run; feed left unchanged")
        return 0
    items, skipped = extract(data)
    undated = [i["url"] for i in items if not i["published_ms"]]
    if undated:
        log(f"mako: {len(undated)} item(s) without an explicit date (kept, without pubDate): {undated}")
    changed = write_if_changed(OUT_DIR / "feed.xml", build_rss(items))
    if changed:
        (OUT_DIR / "build.txt").write_text(str(now_ms), encoding="utf-8")
    log(f"mako: items={len(items)} (main={sum(1 for i in items if i['position'] == 'main')}, "
        f"slider={sum(1 for i in items if i['position'] == 'slider')}) skipped={len(skipped)} changed={changed}")
    for s in skipped:
        log(f"  mako skipped: {s}")
    for i in items:
        log(f"  mako {i['position']:6} {i['url']}  {i['title'][:60]}")
    return 0


def wait_for_pages(build_id):
    url = f"{FEED_BASE_URL}/mako/build.txt"
    deadline = time.time() + PAGES_WAIT_SECONDS
    while time.time() < deadline:
        try:
            _, body = http_get(f"{url}?t={int(time.time())}", timeout=20, headers={"Cache-Control": "no-cache"})
            if body.decode("utf-8").strip() == build_id:
                log(f"mako: pages is live with build {build_id}")
                return True
        except (HTTPError, URLError):
            pass
        time.sleep(15)
    log("mako: timed out waiting for GitHub Pages; pinging anyway")
    return False


def ping_websub():
    body = urlencode([("hub.mode", "publish"), ("hub.url", feed_url())]).encode()
    for attempt in (1, 2, 3):
        req = Request(HUB_URL, data=body, headers={"User-Agent": USER_AGENT,
                                                   "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(req, timeout=30) as r:
                log(f"mako: WebSub hub -> HTTP {r.status} (204 = accepted) for {feed_url()}")
                return r.status in (200, 202, 204)
        except HTTPError as e:
            log(f"mako: WebSub hub error HTTP {e.code} (attempt {attempt}/3): {e.read()[:200]!r}")
            if e.code < 500:
                return False
        except URLError as e:
            log(f"mako: WebSub hub unreachable (attempt {attempt}/3): {e}")
        time.sleep(20)
    return False


def cmd_ping():
    if not FEED_BASE_URL:
        log("FEED_BASE_URL not set; cannot ping.")
        return 1
    build_file = OUT_DIR / "build.txt"
    if build_file.exists():
        wait_for_pages(build_file.read_text(encoding="utf-8").strip())
    return 0 if ping_websub() else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    sys.exit({"build": cmd_build, "ping": cmd_ping}[cmd]())
