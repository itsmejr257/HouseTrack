# Punggol Blk 301–303 watcher

Checks PropertyGuru every night for units on sale at Blk 301A–D, 302A–D and 303A–D
Punggol (the Coralinus blocks) and shows them on a small web page on your NAS.

Two containers:

| Container | What it does |
|---|---|
| `scraper` | Runs once a day at 00:00 Singapore time and writes `data/results.json` |
| `web` | nginx serving the page on port 8089 |

Scraping is done with [kleash/propertyguru-scraper](https://github.com/kleash/propertyguru-scraper),
which provides the Scrapling stealth browser (it clears Cloudflare) and the parser for the
listing data PropertyGuru embeds in its pages. That repo is downloaded at first start rather
than vendored here.

## The page

- Units grouped under 301 / 302 / 303 tiles, which double as filters
- Sort by price, by when the unit was first seen, or **Date listed**, which groups units
  under a heading per listing date, newest first
- A **Logs** tab reading `data/scraper.log`, so you can see what the last run did without SSH
- Units first seen in the last day and a half get a **New** tag
- If a block can't be checked, its units from the previous run stay visible with a
  **Not rechecked** tag, and a banner names the block

## Where it looks

1. **Each block's own listings page**, e.g. `/property-for-sale/at-302c-punggol-place-21369`.
   The page ID for each block is looked up once and cached in `data/block_ids.json`.
2. **A sweep of all Punggol HDB listings**, filtered by address. This catches units an agent
   listed as plain "303 Punggol Central" without the block letter, which never appear on a
   block page. Set `SWEEP: "false"` to skip it and finish faster.

## Install (ZimaOS / CasaOS)

1. Copy this folder to `/DATA/AppData/pg-punggol`.
2. In the dashboard, choose **Install a customized app** and import `docker-compose.yml`.
   Or over SSH: `cd /DATA/AppData/pg-punggol && docker compose up -d`.
3. Open `http://<nas-ip>:8089`.

The first start takes a few minutes: `scraper/start.sh` downloads the upstream repo, installs
Scrapling and fetches Chromium into `runtime/`. Later restarts skip straight to scraping.

Any Docker host works; change the paths in `docker-compose.yml` if yours differ.

## Settings

All in the `environment:` block of `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `RUN_AT` | `00:00` | Daily run time, 24h |
| `MODES` | `sale` | `sale`, `rent`, or `sale,rent` |
| `RUN_ON_START` | `true` | Also scrape when the container starts |
| `SWEEP` | `true` | Also scan all Punggol HDB listings |
| `DELAY` | `5` | Seconds between page loads |
| `BLOCKS` | 301A–303D | Comma-separated blocks to watch |
| `TZ` | `Asia/Singapore` | Timezone for the schedule and timestamps |

Watching different blocks means changing `BLOCKS` and the `STREET_DIR` map in
`scraper/punggol_scraper.py`, which says which street directory page each block number sits on.

## Running the scraper by hand

```bash
docker exec pg-punggol-scraper python /app/punggol_scraper.py --discover   # print block page URLs
docker exec pg-punggol-scraper python /app/punggol_scraper.py --no-sweep   # block pages only
docker exec pg-punggol-scraper python /app/punggol_scraper.py --mode rent
```

## Troubleshooting

- **`ERROR: No Cloudflare challenge found`** — normal. Scrapling says this when a page loads
  without a challenge to solve.
- **`still blocked after 3 retries`** — PropertyGuru refused that page. The HTML it returned is
  saved under `data/debug/` for inspection.
- **Page says the first check hasn't finished** — the run is still going; it takes 10–15 minutes
  with the sweep on.
- **Units show under "Date not shown"** — PropertyGuru worded the posted-on date in a way
  `parse_listed_on()` doesn't recognise yet.

## Files

```
docker-compose.yml        both containers
scraper/start.sh          first-run install of the upstream repo + Scrapling
scraper/punggol_scraper.py  block pages, Punggol sweep, address matching
scraper/scheduler.py      daily schedule, results.json, logging
web/index.html            the page (no build step, plain HTML/CSS/JS)
data/                     results.json, block_ids.json, scraper.log, debug/ (gitignored)
runtime/                  installed dependencies and Chromium (gitignored)
```

## Notes

For personal use. Please respect
[PropertyGuru's terms](https://www.propertyguru.com.sg/terms-and-conditions) and keep the
request delay in place. Licensed under the MIT License; upstream
kleash/propertyguru-scraper is MIT licensed too.
