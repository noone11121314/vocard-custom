#!/usr/bin/env bash
# Randomized multi-dashboard sync test (Patch 34). Usage: fuzz_sync.sh [seeds] [steps]
set -euo pipefail
cd "$(dirname "$0")"
seeds=${1:-30}; steps=${2:-60}
mkdir -p .scripts .fuzz && rm -f .fuzz/*.jsonl
for f in utils.js transformer.js; do docker cp "vocard-dashboard:/app/assets/js/$f" ".scripts/$f" >/dev/null; done
cp ../overrides/assets/js/{websocket,objects,action}.js .scripts/
docker run --rm -v /root/vocard/vocard-src:/app -v "$PWD":/tests -w /app vocard-patched:latest \
  python3 /tests/generate_sync_stream.py /tests/fixture_track_ids.json /tests/.fuzz/seed- --fuzz 1 "$seeds" "$steps"
docker run --rm -v "$PWD":/t -w /t node:20-slim sh -c '
  [ -d node_modules/jsdom ] || npm install --silent --no-audit --no-fund jsdom@24.1.3 jquery@3.7.1 >/dev/null
  pass=0; fail=0
  for f in .fuzz/*.jsonl; do
    out=$(node sync_harness.js .scripts "$f" 2>&1) || true
    if echo "$out" | grep -q " 0 failure"; then pass=$((pass+1)); else fail=$((fail+1)); echo "== $f"; echo "$out" | sed -n 2,4p; fi
  done
  echo "fuzz: $pass random sessions in sync, $fail out of sync"
  [ "$fail" -eq 0 ]'
