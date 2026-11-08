#!/usr/bin/env bash
# Apply dashboard JS changes WITHOUT restarting the dashboard (Patch 31):
# re-runs the dashboard's own minifier inside the running container. The page
# loader in index.html looks up each script's current ETag on every load, so
# the new code reaches each browser on its next page load. Template (.html)
# or compose changes still need dashboard/restart_dashboard.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in dashboard/overrides/assets/js/*.js; do
  docker run --rm -v "$PWD/$f:/f.js:ro" node:20-slim node --check /f.js
done
# Patch 55: an editor that saves by writing a new file (rather than in place)
# leaves the container's single-file mount on the old copy. Only a restart
# re-attaches it, so say so instead of quietly "applying" the old code.
for f in dashboard/overrides/assets/js/*.js; do
  n=$(basename "$f" .js)
  if [ "$(sha256sum < "$f")" != "$(docker exec vocard-dashboard sh -c "sha256sum < /app/assets/js/$n.js")" ]; then
    echo "the dashboard still sees an old $n.js (the file was replaced, not edited in place) - run dashboard/restart_dashboard.sh instead" >&2
    exit 1
  fi
done
docker exec -w /app vocard-dashboard python -c "import main, utils; utils.process_js_files()" >/dev/null
for f in dashboard/overrides/assets/js/*.js; do
  n=$(basename "$f" .js)
  docker exec vocard-dashboard sh -c "test /app/static/js/$n.min.js -nt /app/assets/js/$n.js" || { echo "$n.min.js was not rebuilt" >&2; exit 1; }
done
echo "dashboard JS applied (no restart)"
