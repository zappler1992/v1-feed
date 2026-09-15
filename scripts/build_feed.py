#!/usr/bin/env python3
"""
v1-feed: polls the mako V1 mobile-app JSON, tracks which pages are new,
and generates:
  docs/feed.xml          RSS 2.0 with a WebSub hub declaration
  docs/news-sitemap.xml  Google News sitemap (last 48h, max 1000 URLs)
  docs/sitemap.xml       regular sitemap (all known URLs, lastmod only when explicit)
  docs/build.txt         build id used to confirm GitHub Pages deployed

Dates policy (strict): a page enters the RSS feed / news sitemap ONLY if it
has an explicit publish date, taken from
  1. `publishDate` in the mobile-app JSON, or
  2. `uploadDate` of the JSON-LD VideoObject on the page itself, whose `url`
     equals the page URL.
Pages with neither are listed only in the plain sitemap.xml, without lastmod.

Usage:
  python scripts/build_feed.py build   # fetch + diff + write files
  python scripts/build_feed.py ping    # wait for Pages, then ping the WebSub hub
"""
import json
import os
import re
import sys
import time
import html
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError

try:  # optional: use the OS certificate store (helps on Windows behind TLS-inspecting proxies)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STATE_FILE = ROOT / "state" / "seen.json"

# ---- configuration (override via environment) ------------------------------
SOURCE_URL = os.environ.get("SOURCE_URL", "https://www.mako.co.il/v1/?platform=mobileApp")
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").rstrip("/")   # e.g. https://user.github.io/v1-feed
SITE_HOST = os.environ.get("SITE_HOST", "www.v-1.co.il")
PUBLICATION_NAME = os.environ.get("PUBLICATION_NAME", "V1")
PUBLICATION_LANG = os.environ.get("PUBLICATION_LANG", "he")
FEED_TITLE = os.environ.get("FEED_TITLE", "V1 - כל מה שעלה לאוויר")
FEED_DESCRIPTION = os.environ.get("FEED_DESCRIPTION", "עדכון אוטומטי של דפים חדשים באתר v-1.co.il")
HUB_URL = os.environ.get("HUB_URL", "https://pubsubhubbub.appspot.com/")
INDEXNOW_KEY = os.environ.get("INDEXNOW_KEY", "")                # optional, Bing/Yandex
MAX_FEED_ITEMS = int(os.environ.get("MAX_FEED_ITEMS", "100"))
NEWS_WINDOW_HOURS = int(os.environ.get("NEWS_WINDOW_HOURS", "48"))
NEWS_MAX_URLS = 1000
ENRICH_FROM_PAGE = os.environ.get("ENRICH_FROM_PAGE", "1") == "1"  # read uploadDate from the page's JSON-LD
ENRICH_MAX_PER_RUN = int(os.environ.get("ENRICH_MAX_PER_RUN", "30"))
ENRICH_MAX_AGE_DAYS = int(os.environ.get("ENRICH_MAX_AGE_DAYS", "7"))  # only fetch pages that look this recent
ENRICH_MAX_ATTEMPTS = 3
FUTURE_GRACE_MS = 10 * 60 * 1000   # a publish date further ahead than this is a source error
PAGES_WAIT_SECONDS = int(os.environ.get("PAGES_WAIT_SECONDS", "240"))
USER_AGENT = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Mobile Safari/537.36 v1-feed-bot")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, timeout=60, headers=None):
    req = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---- source parsing ---------------------------------------------------------
def normalize_url(u):
    p = urlsplit(u.strip())
    return urlunsplit((p.scheme or "https", p.netloc.lower(), p.path, "", ""))


def text_of(v):
    """title/subtitle come either as plain strings or {"text": "..."}"""
    if isinstance(v, dict):
        v = v.get("text")
    return (v or "").strip() if isinstance(v, str) else ""


def as_ms(v):
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def extract_items(data):
    """
    Walk the whole JSON; every dict carrying a shareUrl on SITE_HOST is a page.
    The same page appears several times (teaser + feedItem), so merge by URL,
    keeping the richest values. Only `publishDate` / `updateDate` count as dates.
    """
    found = {}

    def merge(url, cand):
        cur = found.setdefault(url, {"url": url, "title": "", "description": "", "image": "",
                                     "type": "", "author": "", "publish_ms": None, "update_ms": None})
        for k in ("title", "description", "image", "type", "author"):
            if cand[k] and len(cand[k]) > len(cur[k]):
                cur[k] = cand[k]
        if cand["publish_ms"]:
            cur["publish_ms"] = cand["publish_ms"] if cur["publish_ms"] is None else min(cur["publish_ms"], cand["publish_ms"])
        if cand["update_ms"]:
            cur["update_ms"] = cand["update_ms"] if cur["update_ms"] is None else max(cur["update_ms"], cand["update_ms"])

    def walk(o):
        if isinstance(o, dict):
            su = o.get("shareUrl")
            if isinstance(su, str) and SITE_HOST in su:
                merge(normalize_url(su), {
                    "title": text_of(o.get("title")),
                    "description": text_of(o.get("description")) or text_of(o.get("subtitle")),
                    "image": o.get("firstFrameImage") if isinstance(o.get("firstFrameImage"), str)
                             else (o.get("image") if isinstance(o.get("image"), str) else ""),
                    "type": o.get("type") if isinstance(o.get("type"), str) else "",
                    "author": o.get("author") if isinstance(o.get("author"), str) else "",
                    "publish_ms": as_ms(o.get("publishDate")),
                    "update_ms": as_ms(o.get("updateDate")),
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    return found


# ---- page enrichment: explicit uploadDate from the page's JSON-LD -----------
LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def page_publish_ms(url):
    """Return the explicit uploadDate/datePublished (ms) of the JSON-LD object whose url matches, else None."""
    try:
        _, body = http_get(url, timeout=30)
    except (HTTPError, URLError, TimeoutError) as e:
        log(f"  enrich: fetch failed {url}: {e}")
        return None
    text = body.decode("utf-8", errors="replace")
    for m in LD_RE.finditer(text):
        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        for obj in (ld if isinstance(ld, list) else [ld]):
            if not isinstance(obj, dict):
                continue
            obj_url = obj.get("url") or obj.get("@id") or ""
            if isinstance(obj_url, str) and normalize_url(obj_url) == url:
                for k in ("datePublished", "uploadDate"):
                    v = obj.get(k)
                    if isinstance(v, str) and v:
                        try:
                            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
                        except ValueError:
                            pass
    return None


def enrich_missing_dates(pages, now_ms):
    """Fetch pages lacking an explicit date (newest-looking first), limited per run."""
    if not ENRICH_FROM_PAGE:
        return 0
    # image paths contain /YYYY/MM/DD/ – used ONLY to decide which pages are worth fetching
    # and in what order, never as a publish date.
    def hint(p):
        m = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", p.get("image") or "")
        return "".join(m.groups()) if m else ""
    min_hint = datetime.fromtimestamp(now_ms / 1000 - ENRICH_MAX_AGE_DAYS * 86400, tz=timezone.utc).strftime("%Y%m%d")
    todo = [p for p in pages.values()
            if not p.get("published_ms") and p.get("enrich_attempts", 0) < ENRICH_MAX_ATTEMPTS
            and (hint(p) == "" or hint(p) >= min_hint)]
    todo.sort(key=lambda p: (hint(p), p.get("first_seen_ms", 0)), reverse=True)
    got = 0
    for p in todo[:ENRICH_MAX_PER_RUN]:
        p["enrich_attempts"] = p.get("enrich_attempts", 0) + 1
        ms = page_publish_ms(p["url"])
        if ms:
            p["published_ms"] = ms
            p["date_source"] = "page-jsonld"
            if not p.get("updated_ms"):
                p["updated_ms"] = ms
            got += 1
    if todo:
        log(f"enrich: tried {min(len(todo), ENRICH_MAX_PER_RUN)} pages, got explicit dates for {got}")
    return got


# ---- state ------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"bootstrapped": False, "pages": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def rfc822(ms):
    return format_datetime(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


# ---- output writers ---------------------------------------------------------
def esc(s):
    return html.escape(s or "", quote=True)


def write_if_changed(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def dated(pages):
    """Pages with an explicit publish date that is not in the future (a future date is a source error)."""
    limit = int(time.time() * 1000) + FUTURE_GRACE_MS
    return [p for p in pages.values() if p.get("published_ms") and p["published_ms"] <= limit]


def build_rss(pages, now_ms):
    items = sorted(dated(pages), key=lambda p: -p["published_ms"])[:MAX_FEED_ITEMS]
    self_url = f"{FEED_BASE_URL}/feed.xml"
    last_build = items[0]["published_ms"] if items else now_ms
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
           'xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">',
           '<channel>',
           f'<title>{esc(FEED_TITLE)}</title>',
           f'<link>https://{SITE_HOST}/</link>',
           f'<description>{esc(FEED_DESCRIPTION)}</description>',
           f'<language>{PUBLICATION_LANG}</language>',
           f'<lastBuildDate>{rfc822(last_build)}</lastBuildDate>',
           f'<atom:link rel="hub" href="{esc(HUB_URL)}"/>',
           f'<atom:link rel="self" href="{esc(self_url)}" type="application/rss+xml"/>']
    for p in items:
        out.append('<item>')
        out.append(f'<title>{esc(p["title"] or p["url"])}</title>')
        out.append(f'<link>{esc(p["url"])}</link>')
        out.append(f'<guid isPermaLink="true">{esc(p["url"])}</guid>')
        out.append(f'<pubDate>{rfc822(p["published_ms"])}</pubDate>')
        if p.get("description"):
            out.append(f'<description>{esc(p["description"])}</description>')
        if p.get("author"):
            out.append(f'<dc:creator>{esc(p["author"])}</dc:creator>')
        if p.get("image"):
            out.append(f'<media:content url="{esc(p["image"])}" medium="image"/>')
        out.append('</item>')
    out += ['</channel>', '</rss>', '']
    return "\n".join(out), len(items)


def build_news_sitemap(pages, now_ms):
    cutoff = now_ms - NEWS_WINDOW_HOURS * 3600 * 1000
    items = sorted((p for p in dated(pages) if p["published_ms"] >= cutoff),
                   key=lambda p: -p["published_ms"])[:NEWS_MAX_URLS]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9" '
           'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    for p in items:
        out.append('<url>')
        out.append(f'<loc>{esc(p["url"])}</loc>')
        out.append('<news:news>')
        out.append(f'<news:publication><news:name>{esc(PUBLICATION_NAME)}</news:name>'
                   f'<news:language>{PUBLICATION_LANG}</news:language></news:publication>')
        out.append(f'<news:publication_date>{iso(p["published_ms"])}</news:publication_date>')
        out.append(f'<news:title>{esc(p["title"] or p["url"])}</news:title>')
        out.append('</news:news>')
        if p.get("image"):
            out.append(f'<image:image><image:loc>{esc(p["image"])}</image:loc></image:image>')
        out.append('</url>')
    out += ['</urlset>', '']
    return "\n".join(out), len(items)


def build_sitemap(pages):
    items = sorted(pages.values(), key=lambda p: -(p.get("updated_ms") or p.get("published_ms") or 0))[:50000]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in items:
        out.append('<url>')
        out.append(f'<loc>{esc(p["url"])}</loc>')
        lm = p.get("updated_ms") or p.get("published_ms")
        if lm:
            out.append(f'<lastmod>{iso(lm)}</lastmod>')
        out.append('</url>')
    out += ['</urlset>', '']
    return "\n".join(out), len(items)


def build_index_html(counts, newest_ms):
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>v1-feed</title>
<link rel="alternate" type="application/rss+xml" title="{esc(FEED_TITLE)}" href="feed.xml">
</head><body>
<h1>v1-feed</h1>
<p>הפריט האחרון עם תאריך מפורש: {esc(iso(newest_ms)) if newest_ms else "-"}</p>
<ul>
<li><a href="feed.xml">feed.xml</a> — RSS ({counts['rss']} פריטים עם תאריך מפורש)</li>
<li><a href="news-sitemap.xml">news-sitemap.xml</a> — מפת אתר ניוז ({counts['news']} כתובות ב-{NEWS_WINDOW_HOURS} השעות האחרונות)</li>
<li><a href="sitemap.xml">sitemap.xml</a> — מפת אתר מלאה ({counts['sitemap']} כתובות)</li>
</ul>
</body></html>
"""


# ---- commands ---------------------------------------------------------------
def cmd_build():
    if not FEED_BASE_URL:
        log("WARNING: FEED_BASE_URL not set; set it to your GitHub Pages URL (rel=self will be wrong).")
    now_ms = int(time.time() * 1000)
    log(f"fetching {SOURCE_URL}")
    data = None
    for attempt in (1, 2, 3):
        try:
            _, body = http_get(SOURCE_URL)
            data = json.loads(body.decode("utf-8"))
            break
        except (json.JSONDecodeError, UnicodeDecodeError, HTTPError, URLError, TimeoutError) as e:
            log(f"source fetch/parse failed (attempt {attempt}/3): {type(e).__name__}: {str(e)[:120]}")
            time.sleep(15)
    if data is None:
        log("source unavailable this run; nothing changed")
        return 0
    found = extract_items(data)
    log(f"source has {len(found)} unique pages on {SITE_HOST}, "
        f"{sum(1 for i in found.values() if i['publish_ms'])} with explicit publishDate")

    state = load_state()
    pages = state["pages"]
    bootstrap = not state.get("bootstrapped")
    new_urls, updated = [], 0

    for url, it in found.items():
        cur = pages.get(url)
        if cur is None:
            pages[url] = {
                "url": url, "title": it["title"], "description": it["description"],
                "image": it["image"], "type": it["type"], "author": it["author"],
                "published_ms": it["publish_ms"],
                "date_source": "json-publishDate" if it["publish_ms"] else None,
                "updated_ms": it["update_ms"] or it["publish_ms"],
                "first_seen_ms": now_ms,
            }
            new_urls.append(url)
        else:
            changed = False
            for k in ("title", "description", "image", "type", "author"):
                if it[k] and it[k] != cur.get(k):
                    cur[k] = it[k]; changed = True
            if it["publish_ms"]:
                stored = cur.get("published_ms")
                stored_is_future = stored and stored > now_ms + FUTURE_GRACE_MS
                if not stored or it["publish_ms"] < stored or stored_is_future:
                    # publication date = the EARLIEST explicit date ever seen for this page. A later
                    # publishDate in the source means the item was re-published, not newly published;
                    # a stored date in the future is a source error and is replaced.
                    if stored:
                        log(f"  date corrected: {url}  {iso(stored)} -> {iso(it['publish_ms'])}")
                    cur["published_ms"] = it["publish_ms"]; cur["date_source"] = "json-publishDate"; changed = True
                elif it["publish_ms"] > stored and it["publish_ms"] > (cur.get("updated_ms") or 0):
                    # re-published later: keep the original date, record the bump as an update
                    cur["updated_ms"] = it["publish_ms"]; changed = True
            if it["update_ms"] and it["update_ms"] != cur.get("updated_ms"):
                cur["updated_ms"] = it["update_ms"]; changed = True
            if changed:
                updated += 1

    enriched = enrich_missing_dates(pages, now_ms)

    state["bootstrapped"] = True
    # only keep fields that change when content changes, so a no-op run leaves the repo untouched
    state.pop("last_run_ms", None)
    if new_urls or bootstrap:
        state["last_new_urls"] = [u for u in new_urls if pages[u].get("published_ms")] if not bootstrap else []
    save_state(state)

    rss, n_rss = build_rss(pages, now_ms)
    news, n_news = build_news_sitemap(pages, now_ms)
    sm, n_sm = build_sitemap(pages)
    changed_any = False
    changed_any |= write_if_changed(DOCS / "feed.xml", rss)
    changed_any |= write_if_changed(DOCS / "news-sitemap.xml", news)
    changed_any |= write_if_changed(DOCS / "sitemap.xml", sm)
    newest = max((p["published_ms"] for p in dated(pages)), default=None)
    write_if_changed(DOCS / "index.html", build_index_html({"rss": n_rss, "news": n_news, "sitemap": n_sm}, newest))
    if changed_any:
        (DOCS / "build.txt").write_text(str(now_ms), encoding="utf-8")
        (DOCS / ".nojekyll").touch()

    n_dated = len(dated(pages))
    future = [p for p in pages.values() if p.get("published_ms") and p["published_ms"] > int(time.time() * 1000) + FUTURE_GRACE_MS]
    for p in future:
        log(f"  future-dated, held back: {p['url']}  {iso(p['published_ms'])}")
    log(f"bootstrap={bootstrap} new={len(new_urls)} updated={updated} enriched={enriched} "
        f"dated={n_dated}/{len(pages)} rss={n_rss} news={n_news} sitemap={n_sm} changed={changed_any}")
    if not bootstrap:
        for u in new_urls[:20]:
            p = pages[u]
            log(f"  NEW [{p.get('date_source') or 'NO DATE - excluded'}] {u}  {p['title'][:70]}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed_any else 'false'}\n")
            f.write(f"new_count={len(new_urls) if not bootstrap else 0}\n")
            f.write(f"ping={'true' if (changed_any and not bootstrap) else 'false'}\n")
    return 0


def wait_for_pages(build_id):
    """Poll the live build.txt until GitHub Pages serves the build we just pushed."""
    url = f"{FEED_BASE_URL}/build.txt"
    deadline = time.time() + PAGES_WAIT_SECONDS
    while time.time() < deadline:
        try:
            _, body = http_get(f"{url}?t={int(time.time())}", timeout=20, headers={"Cache-Control": "no-cache"})
            live = body.decode("utf-8").strip()
            if live == build_id:
                log(f"pages is live with build {build_id}")
                return True
            log(f"pages still serving build {live}, waiting...")
        except (HTTPError, URLError) as e:
            log(f"pages not reachable yet ({e}), waiting...")
        time.sleep(15)
    log("timed out waiting for GitHub Pages; pinging anyway")
    return False


def ping_websub():
    feed_url = f"{FEED_BASE_URL}/feed.xml"
    body = urlencode([("hub.mode", "publish"), ("hub.url", feed_url)]).encode()
    for attempt in (1, 2, 3):
        req = Request(HUB_URL, data=body, headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(req, timeout=30) as r:
                log(f"WebSub hub {HUB_URL} -> HTTP {r.status} (204 = accepted) for {feed_url}")
                return r.status in (200, 202, 204)
        except HTTPError as e:
            log(f"WebSub hub error HTTP {e.code} (attempt {attempt}/3): {e.read()[:200]!r}")
            if e.code < 500:
                return False
        except URLError as e:
            log(f"WebSub hub unreachable (attempt {attempt}/3): {e}")
        time.sleep(20)
    return False


def ping_indexnow(urls):
    if not INDEXNOW_KEY or not urls:
        return
    payload = json.dumps({
        "host": SITE_HOST, "key": INDEXNOW_KEY,
        "keyLocation": f"https://{SITE_HOST}/{INDEXNOW_KEY}.txt",
        "urlList": urls[:10000],
    }).encode("utf-8")
    req = Request("https://api.indexnow.org/indexnow", data=payload,
                  headers={"User-Agent": USER_AGENT, "Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(req, timeout=30) as r:
            log(f"IndexNow -> HTTP {r.status} for {len(urls)} urls")
    except HTTPError as e:
        log(f"IndexNow error HTTP {e.code}: {e.read()[:300]!r}")
    except URLError as e:
        log(f"IndexNow unreachable: {e}")


def cmd_ping():
    if not FEED_BASE_URL:
        log("FEED_BASE_URL not set; cannot ping.")
        return 1
    build_file = DOCS / "build.txt"
    if build_file.exists():
        wait_for_pages(build_file.read_text(encoding="utf-8").strip())
    ok = ping_websub()
    ping_indexnow(load_state().get("last_new_urls", []))
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    sys.exit({"build": cmd_build, "ping": cmd_ping}[cmd]())
