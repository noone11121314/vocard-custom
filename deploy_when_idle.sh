#!/usr/bin/env bash
# Deploys the already-built vocard-patched image once no bot is playing music
# (Lavalink playingPlayers == 0 on two checks 60s apart), so the restart
# never interrupts anyone. Staging first, then production. Gives up after 8h.
set -uo pipefail
cd "$(dirname "$0")"
playing() {
  docker exec vocard python3 -c "
import urllib.request, json
r = urllib.request.Request('http://lavalink:2333/v4/stats', headers={'Authorization': 'youshallnotpass'})
print(json.load(urllib.request.urlopen(r, timeout=10))['playingPlayers'])" 2>/dev/null || echo 1
}
deadline=$(( $(date +%s) + 8*3600 ))
idle=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if [ "$(playing)" = "0" ]; then idle=$((idle+1)); else idle=0; fi
  [ "$idle" -ge 2 ] && break
  sleep 60
done
[ "$idle" -ge 2 ] || { echo "$(date -u +%H:%M) still busy after 8h - not deployed"; exit 1; }
echo "$(date -u +%H:%M:%S) idle - deploying"
docker compose -f docker-compose.staging.yml up -d vocard-staging 2>&1 | tail -1
sleep 10
docker compose up -d vocard 2>&1 | tail -1
sleep 15
docker inspect --format='{{.Name}} {{.Image}}' vocard vocard-staging
docker logs vocard --since 30s 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -E "Connected to dashboard|Error|Traceback" | cut -c12-100
