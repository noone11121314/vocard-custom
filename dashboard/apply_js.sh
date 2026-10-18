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
docker exec -w /app vocard-dashboard python -c "import main, utils; utils.process_js_files()" >/dev/null
for f in dashboard/overrides/assets/js/*.js; do
  n=$(basename "$f" .js)
  docker exec vocard-dashboard sh -c "test /app/static/js/$n.min.js -nt /app/assets/js/$n.js" || { echo "$n.min.js was not rebuilt" >&2; exit 1; }
done
echo "dashboard JS applied (no restart)"
