#!/usr/bin/env bash
# Restart the dashboard the safe way (local-patches.md Patch 29):
#  1. Sets index.html's asset_v to a fingerprint of the dashboard code, so
#     browsers always fetch the new scripts (they're served with a 12h cache).
#  2. Applies compose changes (new mounts) or restarts to re-minify the JS.
#  3. Waits until the dashboard reports healthy.
# Open pages notice the dropped connection and reload themselves.
set -euo pipefail
cd "$(dirname "$0")/.."
base=$(docker image inspect --format '{{.Id}}' ghcr.io/chocomeow/vocard-dashboard:latest)
v=$( (cat dashboard/overrides/assets/js/*.js; echo "$base") | sha1sum | cut -c1-10)
sed -i -E "s/\{% set asset_v = \"[^\"]*\" %\}/{% set asset_v = \"$v\" %}/" dashboard/overrides/templates/index.html
grep -q "asset_v = \"$v\"" dashboard/overrides/templates/index.html || { echo "asset_v not found in index.html" >&2; exit 1; }
if docker compose up -d vocard-dashboard 2>&1 | grep -q "Recreate"; then echo "dashboard recreated"; else docker compose restart vocard-dashboard >/dev/null 2>&1; echo "dashboard restarted"; fi
for _ in $(seq 1 45); do
  [ "$(docker inspect --format='{{.State.Health.Status}}' vocard-dashboard)" = healthy ] && { echo "healthy, asset_v=$v"; exit 0; }
  sleep 2
done
echo "dashboard did not become healthy" >&2; exit 1
