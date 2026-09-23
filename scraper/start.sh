#!/bin/sh
# Runs inside mcr.microsoft.com/playwright/python. On first start it installs
# kleash/propertyguru-scraper and Scrapling into /runtime (a folder on the NAS),
# so later restarts start straight away.
set -e

RUNTIME=/runtime
PGS_COMMIT=66f0f839d1c3ae8544bbac90f0b6486ad5b56c80   # kleash/propertyguru-scraper
SCRAPLING_VERSION=0.4.15
STAMP="$RUNTIME/.installed-$PGS_COMMIT-$SCRAPLING_VERSION"

export PYTHONPATH="$RUNTIME/site:$RUNTIME/propertyguru-scraper:/app"
export PLAYWRIGHT_BROWSERS_PATH="$RUNTIME/browsers"
export PATH="$RUNTIME/site/bin:$PATH"

if [ ! -f "$STAMP" ]; then
  echo "First start: installing kleash/propertyguru-scraper and Scrapling. This takes a few minutes."
  mkdir -p "$RUNTIME"
  rm -rf "$RUNTIME/site" "$RUNTIME/propertyguru-scraper"

  python - "$RUNTIME" "$PGS_COMMIT" <<'PY'
import io, os, sys, tarfile, urllib.request
runtime, commit = sys.argv[1], sys.argv[2]
url = f"https://codeload.github.com/kleash/propertyguru-scraper/tar.gz/{commit}"
print(f"Downloading {url}", flush=True)
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall(runtime)
os.rename(os.path.join(runtime, f"propertyguru-scraper-{commit}"),
          os.path.join(runtime, "propertyguru-scraper"))
PY

  pip install --quiet --disable-pip-version-check --target "$RUNTIME/site" \
      -r "$RUNTIME/propertyguru-scraper/requirements.txt" "scrapling[all]==$SCRAPLING_VERSION"

  # What `scrapling install` does, but keeping the browser in /runtime.
  python -m playwright install chromium
  python -m patchright install chromium || echo "patchright browser install skipped"
  touch "$STAMP"
  echo "Install finished."
fi

# System libraries for the newer Chromium. Needed once per container, not per restart.
if [ ! -f /tmp/.pg-deps-ok ]; then
  if python -m playwright install-deps chromium >/tmp/deps.log 2>&1; then
    touch /tmp/.pg-deps-ok
  else
    echo "Warning: couldn't install Chromium system libraries (see /tmp/deps.log in the container)."
  fi
fi

exec python -u /app/scheduler.py
