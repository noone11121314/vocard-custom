#!/usr/bin/env bash
# How songs were served, per day (Patch 37).
#   played: from Lavalink's logs - every song load is either from the bot's
#           disk cache (http://vocard:2334/stream/...) or streamed straight
#           from YouTube's CDN (googlevideo.com).
#   cached: from the bot's [CacheRoute] log lines (./logs, kept since Patch 37)
#           - which route each new download took, and what couldn't be cached.
# Usage: ./cache_report.sh [days]   (default 14)
set -uo pipefail
cd "$(dirname "$0")"
days=${1:-14}

echo "Songs played - from the cache vs streamed from YouTube (Lavalink logs)"
printf '  %-20s %8s %9s %9s\n' date cached streamed streamed%
for f in $(ls -t lavalink/logs/spring.log* 2>/dev/null | head -n "$days"); do
  case "$f" in
    *.gz) day=$(basename "$f" | sed -E 's/spring\.log\.([0-9-]+)\..*/\1/'); cat_=zcat ;;
    *)    day="$(date -u +%F) (today)"; cat_=cat ;;
  esac
  cached=$($cat_ "$f" | grep -cE "identifier=http(://|%3A%2F%2F)vocard(:|%3A)2334")
  streamed=$($cat_ "$f" | grep -cE "identifier=https?(://|%3A%2F%2F)[a-z0-9.-]+\.googlevideo")
  total=$((cached + streamed))
  pct=$([ "$total" -gt 0 ] && echo "$((100 * streamed / total))%" || echo "-")
  printf '  %-20s %8d %9d %9s\n' "$day" "$cached" "$streamed" "$pct"
done

echo
echo "New downloads - which route cached them (bot [CacheRoute] logs)"
logs=$(ls logs/vocard.log* 2>/dev/null)
if [ -z "$logs" ]; then
  echo "  (no bot logs yet - ./logs is mounted since Patch 37)"
  exit 0
fi
printf '  %-12s %8s %10s %9s %11s\n' date yt-dlp companion streamed not-cached
for d in $(cat $logs | grep -oE '^\S*20[0-9]{2}-[0-9]{2}-[0-9]{2}' | sed -E 's/.*(20[0-9]{2}-[0-9]{2}-[0-9]{2})/\1/' | sort -u | tail -n "$days"); do
  lines=$(cat $logs | sed -E 's/\x1b\[[0-9;]*m//g' | grep "^$d" | grep "\[CacheRoute\]")
  c() { echo "$lines" | grep -c "$1"; }
  printf '  %-12s %8d %10d %9d %11d\n' "$d" "$(c 'via yt-dlp')" "$(c 'via invidious-companion')" "$(c 'streaming ')" "$(c 'could not')"
done
