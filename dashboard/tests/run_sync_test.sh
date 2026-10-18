#!/usr/bin/env bash
# Multi-dashboard sync test (local-patches.md Patch 26). Generates a bot
# message stream from the real Player/Queue code, then replays it into two
# independent dashboards running the real dashboard scripts and checks
# every dashboard against the bot's true state after every message.
# Optional: pass "logs" to also replay the live bot's recorded messages.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .scripts
for f in utils.js websocket.js objects.js transformer.js action.js; do
  docker cp "vocard-dashboard:/app/assets/js/$f" ".scripts/$f" >/dev/null
done
docker run --rm -v /root/vocard/vocard-src:/app -v "$PWD":/tests -w /app vocard-patched:latest \
  python3 /tests/generate_sync_stream.py /tests/fixture_track_ids.json /tests/.stream.jsonl
docker run --rm -v "$PWD":/t -w /t node:20-slim sh -c \
  '[ -d node_modules/jsdom ] || npm install --silent --no-audit --no-fund jsdom@24.1.3 jquery@3.7.1 >/dev/null; node sync_harness.js .scripts .stream.jsonl'
docker run --rm -v "$PWD":/t -w /t node:20-slim node reload_test.js .scripts | grep -E "^(PASS|FAIL)"
