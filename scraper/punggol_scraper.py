#!/usr/bin/env python3
"""
Punggol Blk 301-303 (Coralinus) scraper, built on kleash/propertyguru-scraper.

From that repo it uses:
  * the same Scrapling StealthySession setup (headless, Cloudflare solving,
    ad/WebRTC blocking) and its 15s/30s/60s retry-on-block pattern
  * parsers.parse_page(), which reads the listing data PropertyGuru embeds in
    each page's __NEXT_DATA__ script

Two sources are checked each run:
  1. Each block's own listings page, e.g. /property-for-sale/at-302c-punggol-place-21369
  2. A sweep of all Punggol HDB listings, filtered by address. This catches
     listings an agent tagged as "303 Punggol Central" without the block letter,
     which don't appear on any block page.

Usage:
    python punggol_scraper.py                # for sale
    python punggol_scraper.py --mode rent
    python punggol_scraper.py --no-sweep     # block pages only (faster)
    python punggol_scraper.py --discover     # print each block's page URL
"""

import argparse
import json
import math
import os
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta

import parsers as pg_parsers  # from kleash/propertyguru-scraper (on PYTHONPATH)

BASE = "https://www.propertyguru.com.sg"

# ---------------------------------------------------------------- settings

# Coralinus: 301A-D and 303A-D on Punggol Central, 302A-D on Punggol Place.
DEFAULT_BLOCKS = [f"{n}{s}" for n in ("301", "302", "303") for s in "ABCD"]
BLOCKS = [b.strip().upper() for b in os.getenv("BLOCKS", ",".join(DEFAULT_BLOCKS)).split(",")
          if b.strip()]
BLOCK_NUMBERS = sorted({b[:3] for b in BLOCKS})
STREET_DIR = {
    "301": "punggol-central_108436",
    "302": "punggol-place_142962",
    "303": "punggol-central_108436",
}
# Block page slugs confirmed on the live site; the rest are looked up once and cached.
KNOWN_SLUGS = {
    "301A": "301a-punggol-central-21611",
    "301C": "301c-punggol-central-22087",
    "302C": "302c-punggol-place-21369",
}

SWEEP = os.getenv("SWEEP", "true").lower() == "true"
SWEEP_MAX_PAGES = int(os.getenv("SWEEP_MAX_PAGES", "80"))
DELAY = float(os.getenv("DELAY", "5"))           # same as the repo's download_delay
DATA_DIR = os.getenv("DATA_DIR", ".")
CACHE_PATH = os.path.join(DATA_DIR, "block_ids.json")
DEBUG_DIR = os.path.join(DATA_DIR, "debug")

RETRY_WAIT = [15, 30, 60]  # the repo's backoff when a page comes back blocked

SESSION_OPTIONS = dict(    # identical to the repo's spider / test fixture
    headless=True,
    solve_cloudflare=True,
    block_ads=True,
    block_webrtc=True,
)


logger = logging.getLogger("punggol")


def log(msg):
    logger.info(msg)


LISTED_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y")


def parse_listed_on(text):
    """PropertyGuru's posted-on text -> 'YYYY-MM-DD', or '' if it can't be read."""
    raw = re.sub(r"^(listed|posted)\s+(on\s+)?", "", (text or "").strip(), flags=re.I)
    raw = re.sub(r"\s*\(.*?\)\s*$", "", raw).strip()
    low = raw.lower()
    if low in ("today", "just now"):
        return date.today().isoformat()
    if low == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    m = re.match(r"(\d+)\s*(d|day|days|h|hr|hrs|hour|hours)\s*ago", low)
    if m:
        days = int(m.group(1)) if m.group(2).startswith("d") else 0
        return (date.today() - timedelta(days=days)).isoformat()
    for fmt in LISTED_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


class Blocked(RuntimeError):
    pass


# ---------------------------------------------------------------- fetching

def _html_of(resp):
    raw = resp.body if hasattr(resp, "body") else resp.html_content
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")


def _dump(html, label):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{label}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"  saved page to {path}")
    except Exception:
        pass


def fetch_listing_page(session, url, label):
    """Fetch a listings page; retry like the repo when __NEXT_DATA__ is missing."""
    for attempt in range(len(RETRY_WAIT) + 1):
        try:
            resp = session.fetch(url, google_search=False,
                                 wait_selector="script#__NEXT_DATA__", timeout=60_000)
        except Exception as e:
            log(f"  fetch error: {e}")
            resp = None
        if resp is not None:
            if resp.status == 404:
                return None
            html = _html_of(resp)
            if "__NEXT_DATA__" in html:
                return html
            _dump(html, f"blocked_{label}_attempt{attempt}")
        if attempt < len(RETRY_WAIT):
            wait = RETRY_WAIT[attempt]
            log(f"  no listing data (likely Cloudflare), retry {attempt + 1}/{len(RETRY_WAIT)} in {wait}s")
            time.sleep(wait)
    raise Blocked(f"{label}: still blocked after {len(RETRY_WAIT)} retries")


def fetch_plain_page(session, url):
    try:
        resp = session.fetch(url, google_search=False, timeout=60_000)
    except Exception as e:
        log(f"  fetch error: {e}")
        return None
    return None if resp.status == 404 else _html_of(resp)


# ---------------------------------------------------------------- matching

# "301A", "Blk 302", "block 303c". Not unit numbers like "#12-301" or postal codes.
BLOCK_RE = re.compile(r"(?<![-#\d])\b(?:blk\.?\s*|block\s*)?(30[1-3])([A-Da-d]?)\b", re.I)


def find_block(row):
    """Return e.g. '302C' (or '303' when the agent left out the letter), else None."""
    address = row.get("full_address") or ""
    title = row.get("title") or ""
    url = row.get("listing_url") or ""
    haystack = f"{address} {title} {url}".lower()
    if "punggol" not in haystack:
        return None
    for text in (address, title):
        for m in BLOCK_RE.finditer(text):
            num, letter = m.group(1), m.group(2).upper()
            if num not in BLOCK_NUMBERS:
                continue
            if letter and f"{num}{letter}" not in BLOCKS:
                continue
            return f"{num}{letter}"
    m = re.search(r"-(30[1-3])([a-d]?)-punggol", url)
    if m and m.group(1) in BLOCK_NUMBERS:
        blk = (m.group(1) + m.group(2)).upper()
        if not m.group(2) or blk in BLOCKS:
            return blk
    return None


def to_listing(row, block, mode):
    url = row.get("listing_url") or ""
    if url.startswith("/"):
        url = BASE + url
    price = row.get("price_pretty") or (
        f"S$ {row['price_sgd']:,.0f}" if isinstance(row.get("price_sgd"), (int, float)) else "")
    if price and mode == "rent" and "/mo" not in price:
        price += " /mo"
    area = row.get("floor_area_sqft")
    beds, baths = row.get("bedrooms"), row.get("bathrooms")
    rooms = " ".join(p for p in (f"{beds} bed" if beds else "", f"{baths} bath" if baths else "") if p)
    return {
        "id": str(row.get("listing_id") or url),
        "block": block,
        "price": price,
        "price_sgd": row.get("price_sgd"),
        "address": row.get("full_address") or row.get("title") or f"{block} Punggol",
        "size": " ".join(p for p in (rooms, f"{area:,} sqft" if isinstance(area, (int, float)) else "") if p),
        "listed": f"Listed {row['posted_on']}" if row.get("posted_on") else "",
        "listed_on": parse_listed_on(row.get("posted_on")),
        "agent": row.get("agent_name") or "",
        "url": url,
    }


# ---------------------------------------------------------------- block pages

def load_slugs():
    slugs = dict(KNOWN_SLUGS)
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            slugs.update(json.load(f))
    except Exception:
        pass
    return slugs


def save_slugs(slugs):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(slugs, f, indent=2, sort_keys=True)
    except Exception as e:
        log(f"  couldn't save {CACHE_PATH}: {e}")


def discover_slug(session, block):
    street = STREET_DIR.get(block[:3])
    if not street:
        return None
    url = f"{BASE}/singapore-property-listing/hdb/punggol/{street}/{block.lower()}"
    log(f"[{block}] looking up its page: {url}")
    html = fetch_plain_page(session, url)
    if not html:
        return None
    b = re.escape(block.lower())
    m = (re.search(rf"/project/({b}-[a-z0-9-]+?-\d+)", html, re.I)
         or re.search(rf"/property-for-(?:sale|rent)/at-({b}-[a-z0-9-]+?-\d+)", html, re.I)
         or re.search(rf"/singapore-condo-reviews/({b}-[a-z0-9-]+?-\d+)", html, re.I))
    if not m:
        _dump(html, f"directory_{block}")
    return m.group(1).lower() if m else None


def block_urls(session, mode, stats):
    slugs, changed, urls = load_slugs(), False, {}
    for block in BLOCKS:
        if block not in slugs:
            slug = discover_slug(session, block)
            time.sleep(DELAY)
            if not slug:
                log(f"  couldn't find a PropertyGuru page for {block}")
                stats["failed"].append(block)
                continue
            slugs[block], changed = slug, True
        urls[block] = f"{BASE}/property-for-{mode}/at-{slugs[block]}"
    if changed:
        save_slugs(slugs)
    return urls


def parse(html, label):
    try:
        return pg_parsers.parse_page(html, 1)
    except Exception as e:
        log(f"  couldn't read listing data: {e}")
        _dump(html, f"parse_error_{label}")
        return 0, []


def scrape_block_pages(session, mode, hits, stats):
    for block, url in block_urls(session, mode, stats).items():
        stats["searched"].append(block)
        page = 1
        while True:
            page_url = url if page == 1 else f"{url}?page={page}"
            log(f"[{block}] {page_url}")
            try:
                html = fetch_listing_page(session, page_url, f"{block}_p{page}")
            except Blocked as e:
                log(f"  {e}")
                stats["failed"].append(block)
                break
            if html is None:
                break
            count, rows = parse(html, f"{block}_p{page}")
            new = 0
            for row in rows:
                found = find_block(row)
                # Block pages also show "you might be interested in" listings nearby.
                if not found or found[:3] != block[:3] or (len(found) == 4 and found != block):
                    continue
                listing = to_listing(row, block, mode)
                if listing["id"] not in hits:
                    hits[listing["id"]] = listing
                    new += 1
            log(f"  {len(rows)} listing(s) on page, {new} at {block}")
            time.sleep(DELAY)
            per_page = max(len(rows), 1)
            if page >= math.ceil(count / per_page) or not rows or page >= 5:
                break
            page += 1


def scrape_sweep(session, mode, hits, stats):
    base = f"{BASE}/hdb-for-{mode}/in-punggol"
    stats["searched"].append("Punggol sweep")
    seen, total_pages = set(), None
    for page in range(1, SWEEP_MAX_PAGES + 1):
        url = base if page == 1 else f"{base}?page={page}"
        log(f"[sweep] {url}")
        try:
            html = fetch_listing_page(session, url, f"sweep_p{page}")
        except Blocked as e:
            log(f"  {e}")
            stats["failed"].append("Punggol sweep")
            return
        if html is None:
            break
        count, rows = parse(html, f"sweep_p{page}")
        ids = {r.get("listing_id") for r in rows}
        if not rows or ids <= seen:
            break   # past the last page (or the site ignored ?page=)
        seen |= ids
        if total_pages is None:
            total_pages = math.ceil(count / max(len(rows), 1))
            log(f"  {count} Punggol HDB listings, about {total_pages} pages")
        new = 0
        for row in rows:
            block = find_block(row)
            if not block:
                continue
            listing = to_listing(row, block, mode)
            existing = hits.get(listing["id"])
            if existing is None:
                hits[listing["id"]] = listing
                new += 1
        log(f"  page {page}: {new} new match(es)")
        if total_pages and page >= total_pages:
            break
        time.sleep(DELAY)


def scrape(mode="sale", stats=None, sweep=SWEEP):
    """Return listings at the target blocks. `stats` gets {"searched": [], "failed": []}."""
    from scrapling.fetchers import StealthySession

    # Let Scrapling's own log lines flow into our handlers instead of its own.
    scrapling_log = logging.getLogger("scrapling")
    scrapling_log.handlers.clear()
    scrapling_log.propagate = True

    if stats is None:
        stats = {}
    stats.setdefault("searched", [])
    stats.setdefault("failed", [])
    hits = {}
    with StealthySession(**SESSION_OPTIONS) as session:
        scrape_block_pages(session, mode, hits, stats)
        if sweep:
            scrape_sweep(session, mode, hits, stats)
    return sorted(hits.values(), key=lambda x: (x["block"], x.get("price_sgd") or 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["sale", "rent"], default="sale")
    ap.add_argument("--no-sweep", action="store_true", help="skip the all-Punggol sweep")
    ap.add_argument("--discover", action="store_true", help="only print block page URLs")
    args = ap.parse_args()

    if args.discover:
        from scrapling.fetchers import StealthySession
        stats = {"searched": [], "failed": []}
        with StealthySession(**SESSION_OPTIONS) as s:
            for block, url in block_urls(s, args.mode, stats).items():
                print(f"{block:<5} {url}")
        if stats["failed"]:
            print("Not found:", ", ".join(stats["failed"]))
        return

    stats = {}
    hits = scrape(args.mode, stats, sweep=not args.no_sweep)
    print(f"\nFound {len(hits)} listing(s):\n")
    for h in hits:
        print(f"Blk {h['block']:<5} | {h['price']:<16} | {h['size']:<24} | {h['address']}")
        print(f"            {h['url']}\n")
    if stats["failed"]:
        print("Couldn't check:", ", ".join(stats["failed"]))


if __name__ == "__main__":
    main()
