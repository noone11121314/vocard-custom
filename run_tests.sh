#!/usr/bin/env bash
# Runs every test suite and prints a summary (Patch 34). Exit code is non-zero
# if any suite failed. Needs the stack running (docker compose up) for the
# integration and dashboard suites.
#
#   ./run_tests.sh            everything
#   ./run_tests.sh --quick    unit tests + scripted dashboard sync only
#   ./run_tests.sh --no-youtube   skip the integration tests that download from YouTube
set -uo pipefail
cd "$(dirname "$0")"

QUICK=0; NO_YT=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --no-youtube) NO_YT=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

set -a; [ -f .env ] && . ./.env; set +a
IMAGE=vocard-patched:latest
results=()
run() {  # run <name> <command...>
  local name=$1; shift
  echo; echo "=== $name"
  if "$@"; then results+=("PASS  $name"); else results+=("FAIL  $name"); fi
}

run "unit tests" docker run --rm -v "$PWD/vocard-src:/app" -w /app "$IMAGE" \
  bash -c "pip install -q -r requirements-dev.txt 2>/dev/null; pytest tests/unit -q -p no:cacheprovider"

if [ "$QUICK" -eq 0 ]; then
  docker rm -f vocard-test >/dev/null 2>&1
  yt_filter=""; [ "$NO_YT" -eq 1 ] && yt_filter="--ignore=tests/integration/test_direct_resolve_live.py"
  run "integration tests (real ffmpeg, MongoDB, Lavalink, websocket, restart restore)" \
    docker run --rm --network vocard --name vocard-test --network-alias vocard-test \
      -v "$PWD/vocard-src:/app" -w /app \
      -e INVIDIOUS_COMPANION_URL=http://invidious-companion:8282 \
      -e INVIDIOUS_COMPANION_SECRET="${INVIDIOUS_COMPANION_SECRET:-}" \
      -e STREAM_CACHE_BASE_URL=http://vocard-test:2334 \
      -e VOCARD_TEST_MONGO_URI="mongodb://${MONGO_ROOT_USERNAME:-}:${MONGO_ROOT_PASSWORD:-}@vocard-db:27017" \
      "$IMAGE" bash -c "pip install -q -r requirements-dev.txt 2>/dev/null; pytest tests/integration -q -m integration -p no:cacheprovider $yt_filter"
fi

run "dashboard sync (scripted two-dashboard session + page reload rules)" dashboard/tests/run_sync_test.sh

if [ "$QUICK" -eq 0 ]; then
  run "dashboard sync fuzz (30 random sessions x 60 steps)" dashboard/tests/fuzz_sync.sh 30 60
  run "dashboard page in real Chromium (minified JS)" dashboard/tests/browser_check.sh
fi

echo; echo "=== summary"
printf '%s\n' "${results[@]}"
! printf '%s\n' "${results[@]}" | grep -q '^FAIL'
