#!/usr/bin/env python3
"""
mako full feed + IndexNow submitter (multi-host).

Sources
  www.mako.co.il             every teaser on the homepage app JSON (news, celebs,
                             finance, sport, VOD, recipes, magazine components ...)
  fashionforward.mako.co.il  the site's own WordPress RSS (/feed/), plus any of its
                             pages teased on the mako homepage

Output
  docs/mako/all.xml       RSS 2.0 of those pages, newest first, WebSub hub declared
  state/mako_seen.json    every URL ever seen here + when it was accepted by IndexNow

Every newly seen URL is submitted to IndexNow (Bing/Yandex). IndexNow treats each
host separately, so a host is only submitted once its key file is verified at
https://<host>/<key>.txt. Pages of a host without that file stay queued and go out
automatically on the first run after the file appears. A URL is submitted once;
nothing is resubmitted, per IndexNow's own guidance.

Before a URL is submitted or listed, the page itself is fetched once and checked:
pages carrying `noindex` (meta robots or X-Robots-Tag), or a canonical pointing at a
different URL, are dropped - mako mirrors sport5.co.il articles under /Sports-* and
news-sport that way, and those must not be pushed to a search engine. The verdict is
cached in the state file, so each page is checked once.

Excluded: sponsored/advertising teasers, hosts not listed below, anything the host's
own robots.txt disallows, and anything the page-level check rejects.

Usage:
  python scripts/build_mako_all.py build   # fetch + diff + write feed + submit to IndexNow
  python scripts/build_mako_all.py ping    # wait for Pages, then ping the WebSub hub
"""
import json
import os
import re
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
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
FF_CACHE = ROOT / "state" / "ff_feed_cache.xml"

HOME_URL = os.environ.get("MAKO_SOURCE_URL", "https://www.mako.co.il/?platform=mobileApp")
MAIN_HOST = "www.mako.co.il"
FF_HOST = "fashionforward.mako.co.il"
FF_FEED_URL = os.environ.get("MAKO_FF_FEED_URL", f"https://{FF_HOST}/feed/")
ALLOWED_HOSTS = (MAIN_HOST, FF_HOST)

FEED_TITLE = os.environ.get("MAKO_ALL_FEED_TITLE", "mako - כל מה שבדף הבית")
FEED_DESCRIPTION = os.environ.get(
    "MAKO_ALL_FEED_DESCRIPTION",
    "כל הכתבות המקושרות מדף הבית של mako.co.il ומ-Fashion Forward (ללא תוכן ממומן), מתעדכן כל 5 דקות")
MAX_FEED_ITEMS = int(os.environ.get("MAKO_ALL_MAX_ITEMS", "250"))
IL_TZ = ZoneInfo("Asia/Jerusalem")
PAGES_WAIT_SECONDS = int(os.environ.get("PAGES_WAIT_SECONDS", "240"))

# IndexNow: each host needs the key file at https://<host>/<key>.txt
INDEXNOW_KEY = os.environ.get("MAKO_INDEXNOW_KEY", "d5a26e08f7db8e599910507fb5dc73c4")
INDEXNOW_ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/IndexNow")
INDEXNOW_MAX_PER_REQUEST = 10000
INDEXNOW_ENABLED = os.environ.get("MAKO_INDEXNOW", "1") == "1"

AD_ITEM_MARKERS = ("advertising", "Advertising")
CHECK_MAX_PER_RUN = int(os.environ.get("MAKO_CHECK_MAX_PER_RUN", "60"))
CHECK_MAX_ATTEMPTS = 3
HEAD_BYTES = 400_000          # meta robots / canonical live in <head>
# mako serves a 15 KB javascript stub (no head tags at all) unless a browser-like
# Accept header is sent, so the check must ask for HTML explicitly.
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}
META_ROBOTS_RE = re.compile(
    r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']*)["\']', re.I)
CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.I)
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def feed_url():
    return f"{FEED_BASE_URL}/mako/all.xml"


def parse_local_dt(s):
    """'2026-09-23T15:10' (Israel local time) -> epoch ms, or None."""
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=IL_TZ).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def parse_rfc822(s):
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except (TypeError, ValueError, IndexError):
        return None


def blank_item(url):
    return {"url": url, "title": "", "description": "", "author": "", "flach": "",
            "image": "", "section": "", "content_type": "", "published_ms": None}


# ---- robots -----------------------------------------------------------------
def load_robots(host):
    rp = urllib.robotparser.RobotFileParser()
    try:
        _, body = http_get(f"https://{host}/robots.txt", timeout=30)
        rp.parse(body.decode("utf-8", errors="replace").splitlines())
        return rp
    except (HTTPError, URLError, TimeoutError) as e:
        log(f"mako-all: robots.txt of {host} unavailable ({e}); not filtering on it this run")
        return None


def allowed(url, robots_by_host):
    rp = robots_by_host.get(urlsplit(url).netloc)
    return rp is None or rp.can_fetch("*", url)


# ---- source 1: the mako homepage app JSON -----------------------------------
def is_ad(item):
    itype = item.get("itemType") or ""
    click = (item.get("itemUrl") or {}).get("domoClick") or {}
    return (any(m in itype for m in AD_ITEM_MARKERS)
            or bool(item.get("advertiserName"))
            or click.get("content_type") == "fabricated")


def extract_homepage(data, robots_by_host, found, skipped):
    def merge(cand):
        cur = found.get(cand["url"])
        if cur is None:
            found[cand["url"]] = cand
            return
        for k in ("title", "description", "author", "flach", "image", "section", "content_type"):
            if cand[k] and len(cand[k]) > len(cur[k]):
                cur[k] = cand[k]
        if cand["published_ms"] and (not cur["published_ms"] or cand["published_ms"] < cur["published_ms"]):
            cur["published_ms"] = cand["published_ms"]

    def add(item, section):
        iu = item.get("itemUrl")
        raw = iu.get("url") if isinstance(iu, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            skipped["no_url"] += 1
            return
        if is_ad(item):
            skipped["ad"] += 1
            return
        url = normalize_url(raw)
        if urlsplit(url).netloc not in ALLOWED_HOSTS:
            skipped["other_host"] += 1
            return
        if not allowed(url, robots_by_host):
            skipped["robots"] += 1
            return
        pics = item.get("pics") or []
        cand = blank_item(url)
        cand.update({
            "title": text_of(item.get("title")),
            "description": text_of(item.get("subTitle")) or text_of(item.get("subtitle")),
            "author": text_of(item.get("author")),
            "flach": text_of(item.get("flach")),
            "image": pics[0].get("url") if pics and isinstance(pics[0], dict) else "",
            "section": section,
            "content_type": ((iu.get("domoClick") or {}).get("content_type") or "").lower(),
            "published_ms": parse_local_dt((item.get("date") or {}).get("datetime"))
                            if isinstance(item.get("date"), dict) else None,
        })
        merge(cand)

    def walk(o, section=""):
        if isinstance(o, dict):
            if o.get("componentType"):
                section = text_of(o.get("componentName")) or ""
            if o.get("itemType"):
                add(o, section)
            for v in o.values():
                walk(v, section)
        elif isinstance(o, list):
            for x in o:
                walk(x, section)

    walk(data)


# ---- source 2: the Fashion Forward WordPress RSS ----------------------------
def extract_rss(xml_bytes, host, robots_by_host, found, skipped, default_section):
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            skipped["no_url"] += 1
            continue
        url = normalize_url(link)
        if urlsplit(url).netloc != host:
            skipped["other_host"] += 1
            continue
        if not allowed(url, robots_by_host):
            skipped["robots"] += 1
            continue
        encoded = item.findtext(CONTENT_NS) or ""
        img = IMG_SRC_RE.search(encoded)
        cats = [c.text.strip() for c in item.findall("category") if c.text and c.text.strip()]
        cand = blank_item(url)
        cand.update({
            "title": (item.findtext("title") or "").strip(),
            "description": re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip(),
            "author": (item.findtext(DC_CREATOR) or "").strip(),
            "flach": cats[0] if cats else "",
            "image": img.group(1) if img else "",
            "section": default_section,
            "content_type": "article",
            "published_ms": parse_rfc822(item.findtext("pubDate")),
        })
        cur = found.get(url)
        if cur is None:
            found[url] = cand
        else:  # already teased on the homepage: the site's own RSS has the better data
            for k in ("title", "description", "author", "image"):
                if cand[k] and len(cand[k]) > len(cur[k]):
                    cur[k] = cand[k]
            if cand["published_ms"] and not cur["published_ms"]:
                cur["published_ms"] = cand["published_ms"]


# ---- page-level indexability check ------------------------------------------
def check_page(url):
    """
    Fetch the page once and decide whether it may be listed and submitted.
    Returns (verdict, reason): verdict True/False, or None when the check itself failed
    (network error) and should be retried on a later run.
    """
    req = Request(url, headers=PAGE_HEADERS)
    try:
        with urlopen(req, timeout=45) as r:
            if r.status != 200:
                return False, f"http {r.status}"
            final = normalize_url(r.geturl())
            xrobots = (r.headers.get("X-Robots-Tag") or "").lower()
            head = r.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except HTTPError as e:
        return (False, f"http {e.code}") if 400 <= e.code < 500 else (None, f"http {e.code}")
    except (URLError, TimeoutError, OSError) as e:
        return None, f"{type(e).__name__}"

    if final != url:
        return False, f"redirects to {final}"
    if "noindex" in xrobots:
        return False, "noindex (X-Robots-Tag)"
    m = META_ROBOTS_RE.search(head)
    c = CANONICAL_RE.search(head)
    if not m and not c:
        # neither tag present: we were served a stub or a challenge page, not the article.
        # Retry later rather than assume the page is fine.
        return None, "no robots/canonical tag in response"
    if m and "noindex" in m.group(1).lower():
        return False, "noindex (meta robots)"
    if c:
        canonical = normalize_url(c.group(1).replace("&amp;", "&"))
        if canonical != url:
            return False, f"canonical -> {canonical}"
    return True, "ok"


def check_pending(pages, urls, now_ms):
    """Resolve the indexability of URLs we have not judged yet, oldest first."""
    todo = [u for u in urls
            if pages[u].get("indexable") is None
            and pages[u].get("check_attempts", 0) < CHECK_MAX_ATTEMPTS]
    todo.sort(key=lambda u: pages[u].get("first_seen_ms", 0))
    rejected = 0
    for url in todo[:CHECK_MAX_PER_RUN]:
        p = pages[url]
        p["check_attempts"] = p.get("check_attempts", 0) + 1
        verdict, reason = check_page(url)
        if verdict is None:
            continue
        p["indexable"] = verdict
        p["check_reason"] = reason
        p["checked_ms"] = now_ms
        if not verdict:
            rejected += 1
            log(f"  mako-all SKIP {url}  ({reason})")
    if todo:
        log(f"mako-all: page check: {min(len(todo), CHECK_MAX_PER_RUN)} checked, {rejected} rejected, "
            f"{max(0, len(todo) - CHECK_MAX_PER_RUN)} left for the next run")
    return rejected


# ---- state ------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"pages": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---- IndexNow ---------------------------------------------------------------
def key_verified(host):
    """IndexNow only accepts a host whose key file serves the key itself."""
    key_url = f"https://{host}/{INDEXNOW_KEY}.txt"
    try:
        status, body = http_get(key_url, timeout=30)
    except HTTPError as e:
        log(f"mako-all: IndexNow key missing on {host} (HTTP {e.code} for {key_url}); "
            f"its pages stay queued until the file is added")
        return False
    except (URLError, TimeoutError) as e:
        log(f"mako-all: could not check IndexNow key on {host} ({e}); skipping it this run")
        return False
    if status == 200 and body.decode("utf-8", errors="replace").strip().splitlines()[:1] == [INDEXNOW_KEY]:
        return True
    log(f"mako-all: IndexNow key file on {host} does not contain the key; skipping it")
    return False


def submit_indexnow(host, urls):
    """POST one host's URL list to IndexNow. Returns True when accepted (200/202)."""
    if not urls:
        return True
    if not INDEXNOW_ENABLED:
        log(f"mako-all: IndexNow disabled, would have submitted {len(urls)} url(s) for {host}")
        return False
    if not key_verified(host):
        return False
    ok = True
    for start in range(0, len(urls), INDEXNOW_MAX_PER_REQUEST):
        batch = urls[start:start + INDEXNOW_MAX_PER_REQUEST]
        payload = json.dumps({
            "host": host,
            "key": INDEXNOW_KEY,
            "keyLocation": f"https://{host}/{INDEXNOW_KEY}.txt",
            "urlList": batch,
        }, ensure_ascii=False).encode("utf-8")
        req = Request(INDEXNOW_ENDPOINT, data=payload, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"})
        try:
            with urlopen(req, timeout=60) as r:
                log(f"mako-all: IndexNow {host} -> HTTP {r.status} for {len(batch)} url(s) (200/202 = accepted)")
                ok = ok and r.status in (200, 202)
        except HTTPError as e:
            log(f"mako-all: IndexNow {host} error HTTP {e.code} for {len(batch)} url(s): {e.read()[:300]!r}")
            ok = False
        except (URLError, TimeoutError) as e:
            log(f"mako-all: IndexNow {host} unreachable: {e}")
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
           f'<link>https://{MAIN_HOST}/</link>',
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
def fetch_retry(url, label, want=None, headers=None):
    """Fetch with retries. `want` is a predicate on the body: a response that fails it
    (a bot-challenge page served instead of the real content) counts as a failed attempt."""
    for attempt in (1, 2, 3):
        try:
            req = Request(url, headers=headers or {"User-Agent": USER_AGENT})
            with urlopen(req, timeout=60) as r:
                body = r.read()
            if want is None or want(body):
                return body
            log(f"mako-all: {label} returned {len(body)} bytes that are not the expected "
                f"content (attempt {attempt}/3); likely a bot challenge")
        except (HTTPError, URLError, TimeoutError) as e:
            log(f"mako-all: {label} fetch failed (attempt {attempt}/3): {type(e).__name__}: {str(e)[:120]}")
        time.sleep(15)
    return None


def looks_like_rss(body):
    head = body.lstrip()[:400].lower()
    return head.startswith(b"<?xml") or b"<rss" in head


def fetch_ff_feed():
    """Fashion Forward's feed intermittently serves a captcha page; fall back to the
    last good copy so the output feed does not lose its items for one run."""
    body = fetch_retry(FF_FEED_URL, "fashionforward RSS", want=looks_like_rss, headers=PAGE_HEADERS)
    if body is not None:
        FF_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FF_CACHE.write_bytes(body)
        return body
    if FF_CACHE.exists():
        log("mako-all: using the last cached copy of the fashionforward RSS")
        return FF_CACHE.read_bytes()
    return None


def cmd_build():
    now_ms = int(time.time() * 1000)
    robots_by_host = {h: load_robots(h) for h in ALLOWED_HOSTS}
    found = {}
    skipped = {"ad": 0, "other_host": 0, "robots": 0, "no_url": 0}

    body = fetch_retry(HOME_URL, "homepage")
    if body is None:
        log("mako-all: homepage unavailable this run; feed left unchanged")
        return 0
    try:
        extract_homepage(json.loads(body.decode("utf-8")), robots_by_host, found, skipped)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log(f"mako-all: homepage JSON unparsable ({e}); feed left unchanged")
        return 0
    n_home = len(found)

    ff_body = fetch_ff_feed()
    if ff_body is not None:
        try:
            extract_rss(ff_body, FF_HOST, robots_by_host, found, skipped, "Fashion Forward")
        except ET.ParseError as e:
            log(f"mako-all: fashionforward RSS unparsable ({e}); continuing without it")

    state = load_state()
    pages = state["pages"]
    new_urls = []
    for url, it in found.items():
        cur = pages.get(url)
        if cur is None:
            pages[url] = {
                "title": it["title"], "section": it["section"], "content_type": it["content_type"],
                "published_ms": it["published_ms"], "first_seen_ms": now_ms, "indexnow_ms": None,
                "indexable": None, "check_attempts": 0,
            }
            new_urls.append(url)
        else:
            if it["title"] and it["title"] != cur.get("title"):
                cur["title"] = it["title"]
            if it["published_ms"] and not cur.get("published_ms"):
                cur["published_ms"] = it["published_ms"]

    check_pending(pages, list(found), now_ms)

    # only pages that passed the page-level check reach the feed or IndexNow
    for url in list(found):
        if pages[url].get("indexable") is not True:
            del found[url]

    # everything verified but never accepted by IndexNow, grouped by host
    # (a host without a key file, or a page not yet checked, simply stays queued)
    pending = sorted(u for u, p in pages.items()
                     if p.get("indexable") is True and not p.get("indexnow_ms"))
    by_host = {}
    for u in pending:
        by_host.setdefault(urlsplit(u).netloc, []).append(u)
    submitted = 0
    for host, urls in sorted(by_host.items()):
        if submit_indexnow(host, urls):
            for u in urls:
                pages[u]["indexnow_ms"] = now_ms
            submitted += len(urls)

    state["last_run_ms"] = now_ms
    save_state(state)

    rss, n_items = build_rss(list(found.values()))
    changed = write_if_changed(OUT_DIR / "all.xml", rss)
    if changed:
        (OUT_DIR / "all-build.txt").write_text(str(now_ms), encoding="utf-8")

    hosts = {}
    for u in found:
        hosts[urlsplit(u).netloc] = hosts.get(urlsplit(u).netloc, 0) + 1
    noindex = sum(1 for p in pages.values() if p.get("indexable") is False)
    unchecked = sum(1 for p in pages.values() if p.get("indexable") is None)
    log(f"mako-all: kept={len(found)} per_host={hosts} feed={n_items} new={len(new_urls)} "
        f"submitted={submitted} queued={len(pending) - submitted} unchecked={unchecked} "
        f"noindex_or_canonical={noindex} known={len(pages)} changed={changed} "
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
