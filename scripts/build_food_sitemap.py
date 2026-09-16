#!/usr/bin/env python3
"""
mako food (מאקו אוכל) XML sitemap: a topical sitemap index for every /food-* page on
www.mako.co.il, built from mako's own general sitemap and cleaned by crawling each URL.

Why crawl: mako's general sitemap lists the same recipe under several channel paths,
includes paths that are Disallowed in robots.txt, 404s, and URLs whose canonical points
elsewhere. Only URLs that answer 200, are their own canonical and carry no noindex go in.

Pipeline:
  1. read https://www.mako.co.il/SiteMap/Mako-general-SitemapIndex.xml -> child sitemaps
     -> every <url> whose loc contains mako.co.il/food (keeps the CMS <lastmod>)
  2. crawl each URL (head of the page only): status, final URL, canonical, noindex,
     og:image, JSON-LD dates. Results cached in logs/food/crawl.jsonl and reused
     while younger than --max-age-hours (so a rerun on the same day is fast).
  3. second pass: canonical / redirect targets on /food that the sitemap did not list.
  4. filter + classify by URL path into topical buckets (recipes / articles / hubs).
  5. write docs/mako/food/<bucket>.xml (with image:image) + sitemap-index.xml + report.json

Output URLs: <FEED_BASE_URL>/mako/food/...  (FEED_BASE_URL from env, same as the other builders)

Usage:
  python scripts/build_food_sitemap.py build [--max-age-hours 20] [--threads 10] [--limit N]
"""
import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import ssl
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from gzip import decompress
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "mako" / "food"
STATE_DIR = ROOT / "logs" / "food"          # logs/ is gitignored: the crawl cache stays local
CACHE = STATE_DIR / "crawl.jsonl"
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "https://zappler1992.github.io/v1-feed").rstrip("/")
SITEMAP_BASE = f"{FEED_BASE_URL}/mako/food"
SOURCE_INDEX = "https://www.mako.co.il/SiteMap/Mako-general-SitemapIndex.xml"
ROBOTS_URL = "https://www.mako.co.il/robots.txt"
HOST = "www.mako.co.il"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
HEAD_BYTES = 70_000            # canonical, og:image and the Recipe JSON-LD all sit inside <head>
MAX_URLS_PER_FILE = 50_000     # sitemap protocol limit
W3C_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$")

# ---- topical buckets (order = order in the index file) ------------------------------------
BUCKETS = [
    ("hubs",                 "עמודי קטגוריה וערוצים"),
    ("recipes-baking",       "מתכונים: עוגות, קינוחים, לחמים ומאפים"),
    ("recipes-savory",       "מתכונים: בשר, עוף, דגים, פסטה, מרקים, סלטים וארוחות"),
    ("recipes-healthy",      "מתכונים: בריא, צמחוני, טבעוני, ללא גלוטן"),
    ("recipes-holidays",     "מתכונים: חגים"),
    ("recipes-tv",           "מתכונים: מאסטר שף, בייק אוף, מבשלים עם קשת"),
    ("recipes-general",      "מתכונים: אירוח, מיוחדים, מהירים, משקאות, מגזין"),
    ("articles-restaurants", "כתבות: מסעדות, אוכל רחוב, גורמה"),
    ("articles-holidays",    "כתבות: חגים"),
    ("articles-tv",          "כתבות: תוכניות הבישול"),
    ("articles-magazine",    "כתבות: מגזין, צרכנות, תזונה, טיפים"),
]
HOLIDAY = re.compile(r"shavuot|passover|rosh-hashana|hanukkah|purim|sukkot|tu_bishvat|holiday", re.I)
TV_CHANNELS = {"food-masterchef", "food-masterchef-kids", "food-cooking-with-keshet", "food-cooking-with-barak",
               "food-bake-off-israel", "food-dinner-club", "food-cook-together", "food-neighborhood-cooking",
               "food-flying-cheff", "food-now_playing"}
RESTAURANT_CHANNELS = {"food-restaurants", "food-eating-out", "food-street-food", "food-gourmet",
                       "food-the-tastiest", "food-israels-best-food", "food-timeout"}
BAKING_COLUMNS = {"recipes_column-cakes", "recipes_column-desserts", "recipes_column-bread",
                  "recipes_column-chocolate-cake-recipes", "recipes_column-jams",
                  "recipes_column-bake-off-israel-recipes"}
BAKING_CHANNELS = {"food-bakery", "food-tomers-bread"}
SAVORY_COLUMNS = {"recipes_column-meat", "recipes_column-chicken", "recipes_column-fish-seafood",
                  "recipes_column-pasta", "recipes_column-soups", "recipes_column-salads",
                  "recipes_column-stuffed", "recipes_column-one-pot-meal", "recipes_column-sauces",
                  "recipes_column-dairy_recipes", "recipes_column-asian_recipes", "recipes_column-quiche_recipes"}
HEALTHY_COLUMNS = {"recipes_column-healthy", "recipes_column-vegetarian-recipes", "recipes_column-vegan-recipes",
                   "recipes_column-gluten-free", "recipes_column-sugar-free", "recipes_column-diet"}
HEALTHY_CHANNELS = {"food-nutrition_diet", "food-kids-nutrition"}
BAKING_WORDS = re.compile(r"pastry|sweet|bak|cake|cooki|lotus|chocolate|pudding|cocoa|bread|dessert|pie")
HEALTHY_WORDS = re.compile(r"healthy|vegan|vegetarian|diet|gluten")
SAVORY_WORDS = re.compile(r"meat|fish|chicken|pasta|soup|salad|savory|italian|asian|stuffed")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---- http ---------------------------------------------------------------------------------
_ctx_default = ssl.create_default_context()
_ctx_lax = ssl.create_default_context()
_ctx_lax.check_hostname = False
_ctx_lax.verify_mode = ssl.CERT_NONE


def http_get(url, limit=None, timeout=40):
    """GET with browser headers (mako answers 403 to bare clients). Returns (status, final_url, bytes).
    Falls back to an unverified TLS context: some Windows Python builds reject mako's CA chain."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
                                "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"})
    try:
        r = urlopen(req, timeout=timeout, context=_ctx_default)
    except URLError as e:
        if not isinstance(e.reason, ssl.SSLError):
            raise
        r = urlopen(req, timeout=timeout, context=_ctx_lax)
    with r:
        return r.status, r.geturl(), (r.read(limit) if limit else r.read())


def fetch_xml(url):
    _, _, body = http_get(url)
    if body[:2] == b"\x1f\x8b":
        body = decompress(body)
    return body.decode("utf-8", "replace")


# ---- 1. source urls ------------------------------------------------------------------------
def source_urls():
    """All /food URLs from mako's general sitemap index -> {url: lastmod_or_None}."""
    idx = fetch_xml(SOURCE_INDEX)
    children = re.findall(r"<loc>\s*(.*?)\s*</loc>", idx)
    log(f"source index: {len(children)} child sitemaps")
    urls = {}
    for child in children:
        try:
            x = fetch_xml(child)
        except Exception as e:  # one broken child must not kill the build
            log(f"  skip {child}: {e!r}")
            continue
        for block in re.findall(r"<url>(.*?)</url>", x, re.S):
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block)
            if not loc:
                continue
            loc = html.unescape(loc.group(1))
            if f"{HOST}/food" not in loc:
                continue
            lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block)
            urls.setdefault(loc, lm.group(1) if lm else None)
    log(f"source: {len(urls)} food urls")
    return urls


# ---- robots.txt (User-agent: * group) ------------------------------------------------------
def robots_disallow():
    """Disallow patterns of the `*` group as regexes; `*` wildcard and `$` anchor per Google's spec."""
    try:
        _, _, body = http_get(ROBOTS_URL)
    except Exception as e:
        log(f"robots.txt unavailable ({e!r}); no robots filtering")
        return []
    pats, active = [], False
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            active = val == "*"
        elif key == "disallow" and active and val:
            rx = "^" + "".join(".*" if c == "*" else re.escape(c) for c in val.rstrip("$"))
            if val.endswith("$"):
                rx += "$"
            pats.append(re.compile(rx))
    return pats


def robots_blocked(url, pats):
    s = urlsplit(url)
    path = s.path + (f"?{s.query}" if s.query else "")
    return any(p.match(path) for p in pats)


# ---- 2. crawl ------------------------------------------------------------------------------
def _grab(rx, s):
    m = re.search(rx, s, re.I)
    return html.unescape(m.group(1)).strip() if m else None


def crawl_one(url):
    out = {"url": url, "ts": time.time()}
    for attempt in range(3):
        try:
            status, final, raw = http_get(url, limit=HEAD_BYTES)
            s = raw.decode("utf-8", "ignore")
            out.update(code=status, final=final,
                       canonical=_grab(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', s),
                       noindex=bool(re.search(r'<meta[^>]+name="robots"[^>]+content="[^"]*noindex', s, re.I)),
                       image=_grab(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', s),
                       modified=_grab(r'"dateModified"\s*:\s*"([^"]+)"', s),
                       published=_grab(r'"datePublished"\s*:\s*"([^"]+)"', s),
                       recipe='"@type":"Recipe"' in s.replace(" ", ""))
            return out
        except HTTPError as e:
            out.update(code=e.code, final=e.geturl())
            return out
        except Exception as e:
            out["err"] = repr(e)[:160]
            time.sleep(2 * (attempt + 1))
    return out


def load_cache(max_age_hours):
    cache = {}
    if not CACHE.exists():
        return cache
    cutoff = time.time() - max_age_hours * 3600
    for line in CACHE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", 0) >= cutoff and "code" in rec:
            cache[rec["url"]] = rec
    return cache


def crawl(urls, cache, threads):
    todo = [u for u in urls if u not in cache]
    log(f"crawl: {len(todo)} to fetch, {len(urls) - len(todo)} from cache")
    if not todo:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    t0, n = time.time(), 0
    with CACHE.open("a", encoding="utf-8") as f, cf.ThreadPoolExecutor(threads) as ex:
        for rec in ex.map(crawl_one, todo):
            cache[rec["url"]] = rec
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            if n % 1000 == 0:
                f.flush()
                log(f"  {n}/{len(todo)} ({time.time() - t0:.0f}s)")
    log(f"crawl done in {time.time() - t0:.0f}s")


def compact_cache(cache):
    """Rewrite the cache with one (freshest) line per url so it does not grow forever."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in cache.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(CACHE)


# ---- 4. filter + classify ------------------------------------------------------------------
def path_parts(url):
    return [p for p in urlsplit(url).path.split("/") if p]


def content_id(url):
    m = re.search(r"/(?:Recipe|Article)-([0-9a-f]+)\.htm$", url)
    return m.group(1) if m else None


def indexable(rec):
    """Why a URL is left out, or None if it belongs in the sitemap."""
    if "code" not in rec:
        return "fetch-error"
    if rec["code"] != 200:
        return f"http-{rec['code']}"
    if rec["final"] != rec["url"]:
        return "redirect"
    if rec.get("canonical") and rec["canonical"] != rec["url"]:
        return "canonical-elsewhere"
    if rec.get("noindex"):
        return "noindex"
    return None


def classify(url):
    p = path_parts(url)
    if not p or p[0] != "food" and not p[0].startswith("food-"):
        return None
    last = p[-1]
    if not last.endswith(".htm"):
        return "hubs"
    is_recipe, is_article = last.startswith("Recipe-"), last.startswith("Article-")
    if not (is_recipe or is_article):
        return None
    channel, column = p[0], (p[1] if len(p) > 2 else "")
    holiday = bool(HOLIDAY.search("/".join(p[:-1])))
    if is_recipe:
        if holiday:
            return "recipes-holidays"
        if channel in TV_CHANNELS or column == "recipes_column-masterchef":
            return "recipes-tv"
        if column in BAKING_COLUMNS or channel in BAKING_CHANNELS:
            return "recipes-baking"
        if column in SAVORY_COLUMNS or channel == "food-meals":
            return "recipes-savory"
        if column in HEALTHY_COLUMNS or channel in HEALTHY_CHANNELS:
            return "recipes-healthy"
        rest = "/".join(p[1:-1]).lower()
        if BAKING_WORDS.search(rest):
            return "recipes-baking"
        if HEALTHY_WORDS.search(rest):
            return "recipes-healthy"
        if SAVORY_WORDS.search(rest):
            return "recipes-savory"
        return "recipes-general"
    if channel in RESTAURANT_CHANNELS:
        return "articles-restaurants"
    if holiday:
        return "articles-holidays"
    if channel in TV_CHANNELS:
        return "articles-tv"
    return "articles-magazine"


def best_lastmod(source_lastmod, rec):
    """CMS <lastmod> from mako's sitemap first; JSON-LD dateModified/datePublished as fallback."""
    for cand in (source_lastmod, rec.get("modified"), rec.get("published")):
        if cand and W3C_DATE.match(cand):
            return cand
    return None


# ---- 5. write ------------------------------------------------------------------------------
def esc(s):
    return html.escape(s, quote=False)


def write_if_changed(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def urlset_xml(entries):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
             '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    for e in entries:
        lines.append("  <url>")
        lines.append(f"    <loc>{esc(e['loc'])}</loc>")
        if e.get("lastmod"):
            lines.append(f"    <lastmod>{esc(e['lastmod'])}</lastmod>")
        if e.get("image"):
            lines.append(f"    <image:image><image:loc>{esc(e['image'])}</image:loc></image:image>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def index_xml(files):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for name, lastmod in files:
        lines.append("  <sitemap>")
        lines.append(f"    <loc>{esc(SITEMAP_BASE)}/{name}.xml</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{esc(lastmod)}</lastmod>")
        lines.append("  </sitemap>")
    lines.append("</sitemapindex>")
    return "\n".join(lines) + "\n"


def index_html(report):
    rows = "".join(
        f'<tr><td><a href="{name}.xml">{name}.xml</a></td><td>{label}</td>'
        f'<td>{report["buckets"].get(name, 0):,}</td></tr>'
        for name, label in BUCKETS)
    excluded = "".join(f"<li>{k}: {v:,}</li>" for k, v in sorted(report["excluded"].items(), key=lambda x: -x[1]))
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>mako food sitemap</title>
<style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.4rem;text-align:right}}</style>
</head><body>
<h1>מפת אתר XML – מאקו אוכל</h1>
<p>אינדקס: <a href="sitemap-index.xml">sitemap-index.xml</a> · עודכן {report["built_at"]} · {report["included"]:,} כתובות תקינות מתוך {report["source_urls"]:,} במקור</p>
<table><tr><th>קובץ</th><th>תוכן</th><th>כתובות</th></tr>{rows}</table>
<h2>הושמטו</h2><ul>{excluded}</ul>
<p>מקור: מפת האתר הכללית של mako.co.il, כל כתובת נסרקה ונכנסה רק אם ענתה 200, קנונית לעצמה וללא noindex.</p>
</body></html>
"""


# ---- build ---------------------------------------------------------------------------------
def cmd_build(args):
    source = source_urls()
    if args.limit:
        source = dict(list(source.items())[:args.limit])
    disallow = robots_disallow()
    excluded = Counter()

    candidates = {}
    for url, lm in source.items():
        if robots_blocked(url, disallow):
            excluded["robots-disallow"] += 1
        else:
            candidates[url] = lm

    cache = load_cache(args.max_age_hours)
    crawl(list(candidates), cache, args.threads)

    # second pass: pages the source never listed but that redirects / canonicals point at
    extra = set()
    for url in candidates:
        rec = cache.get(url, {})
        for t in (rec.get("final"), rec.get("canonical")):
            if t and t != url and t not in candidates and t not in extra \
                    and urlsplit(t).netloc == HOST and f"{HOST}/food" in t and not robots_blocked(t, disallow):
                extra.add(t)
    if extra:
        log(f"second pass: {len(extra)} canonical/redirect targets missing from the source sitemap")
        crawl(sorted(extra), cache, args.threads)
        for t in extra:
            candidates[t] = None
    compact_cache(cache)

    buckets = defaultdict(list)
    seen_ids = {}
    for url, lm in candidates.items():
        rec = cache.get(url, {"url": url})
        why = indexable(rec)
        if why:
            excluded[why] += 1
            continue
        bucket = classify(url)
        if not bucket:
            excluded["unclassified"] += 1
            continue
        cid = content_id(url)
        if cid and cid in seen_ids:      # same item, two self-canonical paths: keep the first
            excluded["duplicate-id"] += 1
            continue
        if cid:
            seen_ids[cid] = url
        image = rec.get("image") or ""
        if bucket == "hubs" or not image.startswith("http") or "logoMako" in image:
            image = None                 # hubs only carry the generic mako logo
        buckets[bucket].append({"loc": url, "lastmod": best_lastmod(lm, rec), "image": image})

    files, changed = [], []
    for name, _label in BUCKETS:
        # newest first, then by url: a deterministic order keeps daily diffs to real changes
        entries = sorted(buckets.get(name, []), key=lambda e: e["loc"])
        entries.sort(key=lambda e: e["lastmod"] or "", reverse=True)
        entries = entries[:MAX_URLS_PER_FILE]
        newest = max((e["lastmod"] for e in entries if e["lastmod"]), default=None)
        files.append((name, newest))
        if write_if_changed(OUT_DIR / f"{name}.xml", urlset_xml(entries)):
            changed.append(name)
    if write_if_changed(OUT_DIR / "sitemap-index.xml", index_xml(files)):
        changed.append("sitemap-index")

    report = {
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_urls": len(source),
        "included": sum(len(v) for v in buckets.values()),
        "buckets": {name: len(buckets.get(name, [])) for name, _ in BUCKETS},
        "excluded": dict(excluded),
        "with_image": sum(1 for v in buckets.values() for e in v if e["image"]),
        "with_lastmod": sum(1 for v in buckets.values() for e in v if e["lastmod"]),
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_if_changed(OUT_DIR / "index.html", index_html(report))
    log(f"included {report['included']} / excluded {dict(excluded)}")
    log(f"changed: {changed or 'nothing'}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--max-age-hours", type=float, default=20, help="reuse crawl results younger than this")
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="debug: only the first N source urls")
    args = ap.parse_args()
    return cmd_build(args)


if __name__ == "__main__":
    sys.exit(main())
