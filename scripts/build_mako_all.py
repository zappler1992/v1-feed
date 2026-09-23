#!/usr/bin/env python3
"""
mako full-homepage feed + IndexNow submitter.

Unlike build_mako_home.py (main item + slider only), this covers EVERY item linked
from the mako.co.il homepage app JSON: news, celebs, finance, sport, VOD, recipes,
magazine components - each teaser that points to a real page on www.mako.co.il.

  docs/mako/all.xml       RSS 2.0 of those pages, newest first, WebSub hub declared
  state/mako_seen.json    every URL ever seen here + when it was sent to IndexNow

Every newly seen URL is submitted to IndexNow (Bing/Yandex) using the key verified at
https://www.mako.co.il/<key>.txt. A URL is submitted once; nothing is resubmitted on
later runs, per IndexNow's own guidance.

Excluded: sponsored/advertising teasers, anything not on www.mako.co.il (story.,
fashionforward., auto.co.il ... are separate hosts the key does not cover), and
anything mako's own robots.txt disallows.

Usage:
  python scripts/build_mako_all.py build   # fetch + diff + write feed + submit to IndexNow
  python scripts/build_mako_all.py ping    # wait for Pages, then ping the WebSub hub
"""
import json
import os
import sys
import time
import urllib.robotparser
from datetime import datetime
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
STATE_FILE = ROOT / "state" / "mako_seen.json"

SOURCE_URL = os.environ.get("MAKO_SOURCE_URL", "https://www.mako.co.il/?platform=mobileApp")
SITE_HOST = "www.mako.co.il"
ROBOTS_URL = f"https://{SITE_HOST}/robots.txt"
FEED_TITLE = os.environ.get("MAKO_ALL_FEED_TITLE", "mako - כל מה שבדף הבית")
FEED_DESCRIPTION = os.environ.get(
    "MAKO_ALL_FEED_DESCRIPTION",
    "כל הכתבות המקושרות מדף הבית של mako.co.il (ללא תוכן ממומן), מתעדכן כל 5 דקות")
MAX_FEED_ITEMS = int(os.environ.get("MAKO_ALL_MAX_ITEMS", "200"))
IL_TZ = ZoneInfo("Asia/Jerusalem")
PAGES_WAIT_SECONDS = int(os.environ.get("PAGES_WAIT_SECONDS", "240"))

# IndexNow: the key file must stay reachable at https://www.mako.co.il/<key>.txt
INDEXNOW_KEY = os.environ.get("MAKO_INDEXNOW_KEY", "d5a26e08f7db8e599910507fb5dc73c4")
INDEXNOW_ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/IndexNow")
INDEXNOW_MAX_PER_REQUEST = 10000
INDEXNOW_ENABLED = os.environ.get("MAKO_INDEXNOW", "1") == "1"

AD_ITEM_MARKERS = ("advertising", "Advertising")


def feed_url():
    return f"{FEED_BASE_URL}/mako/all.xml"


def parse_local_dt(s):
    """'2026-09-23T15:10' (Israel local time) -> epoch ms, or None."""
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=IL_TZ).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def load_robots():
    rp = urllib.robotparser.RobotFileParser()
    try:
        _, body = http_get(ROBOTS_URL, timeout=30)
        rp.parse(body.decode("utf-8", errors="replace").splitlines())
        return rp
    except (HTTPError, URLError, TimeoutError) as e:
        log(f"mako-all: robots.txt unavailable ({e}); not filtering on it this run")
        return None


def is_ad(item):
    itype = item.get("itemType") or ""
    click = (item.get("itemUrl") or {}).get("domoClick") or {}
    return (any(m in itype for m in AD_ITEM_MARKERS)
            or bool(item.get("advertiserName"))
            or click.get("content_type") == "fabricated")


def extract(data, robots):
    """Every teaser on the homepage that links to a real, crawlable page on www.mako.co.il."""
    found, skipped = {}, {"ad": 0, "other_host": 0, "robots": 0, "no_url": 0}

    def walk(o, component=None, section=""):
        if isinstance(o, dict):
            if o.get("componentType"):
                component = o["componentType"]
                section = text_of(o.get("componentName")) or ""
            if o.get("itemType"):
                add(o, component, section)
            for v in o.values():
                walk(v, component, section)
        elif isinstance(o, list):
            for x in o:
                walk(x, component, section)

    def add(item, component, section):
        iu = item.get("itemUrl")
        raw = iu.get("url") if isinstance(iu, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            skipped["no_url"] += 1
            return
        if is_ad(item):
            skipped["ad"] += 1
            return
        url = normalize_url(raw)
        if urlsplit(url).netloc != SITE_HOST:
            skipped["other_host"] += 1
            return
        if robots is not None and not robots.can_fetch("*", url):
            skipped["robots"] += 1
            return
        pics = item.get("pics") or []
        cand = {
            "url": url,
            "title": text_of(item.get("title")),
            "description": text_of(item.get("subTitle")) or text_of(item.get("subtitle")),
            "author": text_of(item.get("author")),
            "flach": text_of(item.get("flach")),
            "image": pics[0].get("url") if pics and isinstance(pics[0], dict) else "",
            "section": section,
            "component": component or "",
            "content_type": ((iu.get("domoClick") or {}).get("content_type") or "").lower(),
            "published_ms": parse_local_dt((item.get("date") or {}).get("datetime"))
                            if isinstance(item.get("date"), dict) else None,
        }
        cur = found.get(url)
        if cur is None:
            found[url] = cand
            return
        # same page teased twice: keep the richest text, the earliest date
        for k in ("title", "description", "author", "flach", "image", "section", "content_type"):
            if cand[k] and len(cand[k]) > len(cur[k]):
                cur[k] = cand[k]
        if cand["published_ms"] and (not cur["published_ms"] or cand["published_ms"] < cur["published_ms"]):
            cur["published_ms"] = cand["published_ms"]

    walk(data)
    return found, skipped


# ---- state ------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"pages": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---- IndexNow ---------------------------------------------------------------
def submit_indexnow(urls):
    """POST the URL list to IndexNow. Returns True when accepted (200/202)."""
    if not urls:
        return True
    if not INDEXNOW_ENABLED:
        log(f"mako-all: IndexNow disabled, would have submitted {len(urls)} url(s)")
        return False
    ok = True
    for start in range(0, len(urls), INDEXNOW_MAX_PER_REQUEST):
        batch = urls[start:start + INDEXNOW_MAX_PER_REQUEST]
        payload = json.dumps({
            "host": SITE_HOST,
            "key": INDEXNOW_KEY,
            "keyLocation": f"https://{SITE_HOST}/{INDEXNOW_KEY}.txt",
            "urlList": batch,
        }, ensure_ascii=False).encode("utf-8")
        req = Request(INDEXNOW_ENDPOINT, data=payload, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"})
        try:
            with urlopen(req, timeout=60) as r:
                log(f"mako-all: IndexNow -> HTTP {r.status} for {len(batch)} url(s) "
                    f"(200/202 = accepted)")
                ok = ok and r.status in (200, 202)
        except HTTPError as e:
            body = e.read()[:300]
            log(f"mako-all: IndexNow error HTTP {e.code} for {len(batch)} url(s): {body!r}")
            ok = False
        except (URLError, TimeoutError) as e:
            log(f"mako-all: IndexNow unreachable: {e}")
            ok = False
    return ok


# ---- feed -------------------------------------------------------------------
def build_rss(items):
    ordered = sorted(items, key=lambda p: (p["published_ms"] is None, -(p["published_ms"] or 0)))[:MAX_FEED_ITEMS]
    dates = [p["published_ms"] for p in ordered if p["published_ms"]]
    last_build = max(dates) if dates else int(time.time() * 1000)
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
    for p in ordered:
        out.append('<item>')
        out.append(f'<title>{esc(p["title"] or p["url"])}</title>')
        out.append(f'<link>{esc(p["url"])}</link>')
        out.append(f'<guid isPermaLink="true">{esc(p["url"])}</guid>')
        if p["published_ms"]:
            out.append(f'<pubDate>{rfc822(p["published_ms"])}</pubDate>')
        desc = p["description"] or p["title"] or p["url"]
        out.append(f'<description>{esc(desc)}</description>')
        for cat in (p["section"], p["flach"]):
            if cat:
                out.append(f'<category>{esc(cat)}</category>')
        if p["author"]:
            out.append(f'<dc:creator>{esc(p["author"])}</dc:creator>')
        body = (f'<p><a href="{esc(p["url"])}"><img src="{esc(p["image"])}" alt="{esc(p["title"])}"/></a></p>'
                if p["image"] else "")
        body += f'<p>{esc(desc)}</p>'
        out.append(f'<content:encoded><![CDATA[{body}]]></content:encoded>')
        if p["image"]:
            out.append(f'<media:thumbnail url="{esc(p["image"])}"/>')
            out.append(f'<media:content url="{esc(p["image"])}" medium="image"/>')
        out.append('</item>')
    out += ['</channel>', '</rss>', '']
    return "\n".join(out), len(ordered)


# ---- commands ---------------------------------------------------------------
def cmd_build():
    now_ms = int(time.time() * 1000)
    data = None
    for attempt in (1, 2, 3):
        try:
            _, body = http_get(SOURCE_URL)
            data = json.loads(body.decode("utf-8"))
            break
        except (json.JSONDecodeError, UnicodeDecodeError, HTTPError, URLError, TimeoutError) as e:
            log(f"mako-all: source fetch/parse failed (attempt {attempt}/3): {type(e).__name__}: {str(e)[:120]}")
            time.sleep(15)
    if data is None:
        log("mako-all: source unavailable this run; feed left unchanged")
        return 0

    found, skipped = extract(data, load_robots())
    state = load_state()
    pages = state["pages"]

    new_urls = []
    for url, it in found.items():
        cur = pages.get(url)
        if cur is None:
            pages[url] = {
                "title": it["title"], "section": it["section"], "content_type": it["content_type"],
                "published_ms": it["published_ms"], "first_seen_ms": now_ms, "indexnow_ms": None,
            }
            new_urls.append(url)
        else:
            if it["title"] and it["title"] != cur.get("title"):
                cur["title"] = it["title"]
            if it["published_ms"] and not cur.get("published_ms"):
                cur["published_ms"] = it["published_ms"]

    # anything seen before but never accepted by IndexNow gets retried
    pending = sorted({u for u in new_urls} | {u for u, p in pages.items() if not p.get("indexnow_ms")})
    if pending and submit_indexnow(pending):
        for u in pending:
            pages[u]["indexnow_ms"] = now_ms

    state["last_run_ms"] = now_ms
    save_state(state)

    rss, n_items = build_rss(list(found.values()))
    changed = write_if_changed(OUT_DIR / "all.xml", rss)
    if changed:
        (OUT_DIR / "all-build.txt").write_text(str(now_ms), encoding="utf-8")

    log(f"mako-all: found={len(found)} feed={n_items} new={len(new_urls)} "
        f"submitted={len(pending)} known={len(pages)} changed={changed} "
        f"skipped(ad={skipped['ad']}, other_host={skipped['other_host']}, robots={skipped['robots']})")
    for u in new_urls[:25]:
        log(f"  mako-all NEW {u}  {pages[u]['title'][:60]}")
    return 0


def wait_for_pages(build_id):
    url = f"{FEED_BASE_URL}/mako/all-build.txt"
    deadline = time.time() + PAGES_WAIT_SECONDS
    while time.time() < deadline:
        try:
            _, body = http_get(f"{url}?t={int(time.time())}", timeout=20, headers={"Cache-Control": "no-cache"})
            if body.decode("utf-8").strip() == build_id:
                log(f"mako-all: pages is live with build {build_id}")
                return True
        except (HTTPError, URLError):
            pass
        time.sleep(15)
    log("mako-all: timed out waiting for GitHub Pages; pinging anyway")
    return False


def ping_websub():
    body = urlencode([("hub.mode", "publish"), ("hub.url", feed_url())]).encode()
    for attempt in (1, 2, 3):
        req = Request(HUB_URL, data=body, headers={"User-Agent": USER_AGENT,
                                                   "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(req, timeout=30) as r:
                log(f"mako-all: WebSub hub -> HTTP {r.status} (204 = accepted) for {feed_url()}")
                return r.status in (200, 202, 204)
        except HTTPError as e:
            log(f"mako-all: WebSub hub error HTTP {e.code} (attempt {attempt}/3): {e.read()[:200]!r}")
            if e.code < 500:
                return False
        except (URLError, TimeoutError) as e:
            log(f"mako-all: WebSub hub unreachable (attempt {attempt}/3): {e}")
        time.sleep(20)
    return False


def cmd_ping():
    if not FEED_BASE_URL:
        log("FEED_BASE_URL not set; cannot ping.")
        return 1
    build_file = OUT_DIR / "all-build.txt"
    if build_file.exists():
        wait_for_pages(build_file.read_text(encoding="utf-8").strip())
    return 0 if ping_websub() else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    sys.exit({"build": cmd_build, "ping": cmd_ping}[cmd]())
