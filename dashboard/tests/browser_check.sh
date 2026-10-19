#!/usr/bin/env bash
# Minifies the local dashboard JS with the dashboard's own jsmin, checks the
# minified output parses, renders index.html, and loads it in real Chromium
# (browser_check.js). Run with the dashboard container up.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .min
for f in ../overrides/assets/js/*.js; do
  n=$(basename "$f" .js)
  docker exec -i vocard-dashboard python -c "import sys; from jsmin import jsmin; sys.stdout.write(jsmin(sys.stdin.read(), quote_chars=\"'\\\"\`\"))" < "$f" > ".min/$n.min.js"
done
docker run --rm -v "$PWD/.min:/m" node:20-slim sh -c 'for f in /m/*.min.js; do node --check "$f" || { echo "FAIL  minified $(basename $f) does not parse"; exit 1; }; done; echo "PASS  minified dashboard JS parses"'
docker exec -w /app vocard-dashboard python -c "
import asyncio, types, main
main.LANGUAGES['en'] = {'name': 'English'}
async def r():
    u = types.SimpleNamespace(id='1', name='check', avatar='', avatar_url='', country='VN', language_code='en')
    async with main.app.test_request_context('/'):
        from quart import render_template
        print(await render_template('index.html', user=u, languages=main.LANGUAGES))
asyncio.run(r())" > .min/index.html
docker run --rm --network host -v "$PWD":/t -w /t mcr.microsoft.com/playwright:v1.47.0-jammy sh -c \
  '[ -d /tmp/pw/node_modules/ws ] || npm i --silent --no-audit --no-fund --prefix /tmp/pw playwright@1.47.0 ws@8.18.0 >/dev/null 2>&1; NODE_PATH=/tmp/pw/node_modules node browser_check.js && NODE_PATH=/tmp/pw/node_modules node queue_panel_check.js && NODE_PATH=/tmp/pw/node_modules node multi_tab_check.js'
