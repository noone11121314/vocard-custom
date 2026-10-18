#!/usr/bin/env bash
# Restart the dashboard the safe way (local-patches.md Patch 29):
#  1. Applies compose changes (new mounts) or restarts (re-minifies the JS,
#     reloads the cached page templates).
#  2. Waits until the dashboard reports healthy.
# Only needed for template (.html) or compose changes - JS-only changes go
# live without a restart via dashboard/apply_js.sh (Patch 31).
# Open pages notice the dropped connection and reload themselves.
set -euo pipefail
cd "$(dirname "$0")/.."
if docker compose up -d vocard-dashboard 2>&1 | grep -q "Recreate"; then echo "dashboard recreated"; else docker compose restart vocard-dashboard >/dev/null 2>&1; echo "dashboard restarted"; fi
for _ in $(seq 1 45); do
  [ "$(docker inspect --format='{{.State.Health.Status}}' vocard-dashboard)" = healthy ] && { echo "healthy"; exit 0; }
  sleep 2
done
echo "dashboard did not become healthy" >&2; exit 1
