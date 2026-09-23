#!/usr/bin/env python3
"""
Runs punggol_scraper.scrape() every day at RUN_AT (default 00:00, Singapore time)
and writes the results to $DATA_DIR/results.json for the web UI.

Environment variables (all optional):
    MODES         "sale", "rent" or "sale,rent"          (default: sale)
    RUN_AT        24h time to run, HH:MM                 (default: 00:00)
    TZ            IANA timezone                          (default: Asia/Singapore)
    RUN_ON_START  "true" to also run once on startup     (default: true)
    DELAY         seconds between page loads             (default: 5)
    SWEEP         "false" to skip the all-Punggol sweep  (default: true)
    DATA_DIR      where results.json is written          (default: /data)
"""

import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import punggol_scraper as pg

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ", "Asia/Singapore"))
except Exception:  # no tzdata in the image: Singapore is a fixed UTC+8 with no DST
    TZ = timezone(timedelta(hours=8))

MODES = [m.strip() for m in os.getenv("MODES", "sale").split(",") if m.strip()]
RUN_AT = os.getenv("RUN_AT", "00:00")
RUN_ON_START = os.getenv("RUN_ON_START", "true").lower() == "true"
DATA_DIR = os.getenv("DATA_DIR", "/data")
RESULTS = os.path.join(DATA_DIR, "results.json")
LOG_FILE = os.path.join(DATA_DIR, "scraper.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(1_000_000)))


class _TZFormatter(logging.Formatter):
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, TZ).timetuple()


def setup_logging():
    """Everything (this file, the scraper, Scrapling) goes to stdout and to
    scraper.log, which the web page reads."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = _TZFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.handlers.RotatingFileHandler(
                        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def log(msg):
    logging.getLogger("scheduler").info(msg)


def load_previous():
    try:
        with open(RESULTS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_atomic(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RESULTS)


def run_once():
    started = datetime.now(TZ)
    prev = load_previous()
    first_seen = {l.get("key"): l.get("first_seen") for l in prev.get("listings", [])}

    listings, searched, failed, failed_names, errors = [], 0, 0, [], []
    for mode in MODES:
        stats = {}
        try:
            log(f"Scraping mode={mode}")
            hits = pg.scrape(mode, stats)
        except Exception as e:
            logging.getLogger("scheduler").exception("Scrape failed")
            errors.append(f"{mode}: {e}")
            hits = []
        failed_blocks = set(stats.get("failed", []))
        searched += len(set(stats.get("searched", [])) | failed_blocks)
        failed += len(failed_blocks)
        failed_names += sorted(failed_blocks)
        for h in hits:
            key = f"{mode}:{h['id'] or h['url']}"
            listings.append({
                **h,
                "mode": mode,
                "key": key,
                "first_seen": first_seen.get(key) or started.isoformat(),
            })

        # Blocks PropertyGuru refused this run keep the units found last time,
        # flagged as stale, rather than vanishing from the page.
        if failed_blocks:
            have = {l["key"] for l in listings}
            for old in prev.get("listings", []):
                if (old.get("mode") == mode and old.get("block") in failed_blocks
                        and old.get("key") not in have):
                    listings.append({**old, "stale": True})

    finished = datetime.now(TZ)
    if errors or (searched and failed == searched):
        status = "blocked" if not errors else "error"
    elif failed:
        status = "partial"
    else:
        status = "ok"

    data = {
        "updated_at": finished.isoformat(),
        "status": status,
        "message": "; ".join(errors) if errors else (
            f"Couldn't check {', '.join(dict.fromkeys(failed_names))}" if failed else ""),
        "modes": MODES,
        "last_success_at": finished.isoformat() if status in ("ok", "partial")
                           else prev.get("last_success_at"),
        # A fully blocked run keeps the last good list instead of showing zero units.
        "listings": listings if status in ("ok", "partial") else prev.get("listings", []),
    }
    write_atomic(data)
    log(f"Done: status={status}, {len(data['listings'])} listing(s) "
        f"in {(finished - started).seconds}s")


def seconds_until_next_run():
    hh, mm = (int(x) for x in RUN_AT.split(":"))
    now = datetime.now(TZ)
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds(), nxt


def main():
    setup_logging()
    log(f"Scheduler started: modes={MODES}, daily at {RUN_AT} ({TZ}), sweep={pg.SWEEP}")
    if not os.path.exists(RESULTS):
        write_atomic({"updated_at": None, "status": "pending", "message": "",
                      "modes": MODES, "last_success_at": None, "listings": []})
    if RUN_ON_START:
        run_once()
    while True:
        wait, nxt = seconds_until_next_run()
        log(f"Next run at {nxt:%Y-%m-%d %H:%M}")
        # Sleep in chunks so clock drift or NAS sleep doesn't make us miss the slot badly.
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (nxt - datetime.now(TZ)).total_seconds()
        try:
            run_once()
        except Exception:
            logging.getLogger("scheduler").exception("Run failed")


if __name__ == "__main__":
    sys.exit(main())
