# Vocard Deployment

A self-hosted [Vocard](https://github.com/ChocoMeow/Vocard) Discord music bot
stack, running via Docker Compose with a locally patched bot image and a
single-file dashboard override. **This is a patched deployment, not vanilla
upstream** — see [Local Patches](#local-patches) before assuming stock
behavior or rebuilding anything from a clean upstream clone.

## Services

| Service | Image | Notes |
|---|---|---|
| `vocard` | `vocard-patched:latest` (built locally from `vocard-src/`) | the bot itself |
| `vocard-dashboard` | `ghcr.io/chocomeow/vocard-dashboard:latest` (stock, + one file override) | web dashboard, port 8000 |
| `lavalink` | `ghcr.io/lavalink-devs/lavalink:latest` | audio node |
| `invidious-companion` | `quay.io/invidious/invidious-companion:latest` | resolves direct YouTube stream URLs for the bot's direct-URL-resolver feature |
| `vocard-db` | `mongo:8` | bot + dashboard persistent storage |

See `docker-compose.yml` for the full wiring (env vars, volumes, health checks).

## Layout

```
vocard/
├── docker-compose.yml
├── settings.json              live bot config (bind-mounted into vocard, no hot-reload — restart after editing)
├── vocard-src/                bot source; built into vocard-patched:latest
│   └── docs/                  feature docs + testing guide for the bot
├── dashboard/
│   ├── settings.json          live dashboard config (bind-mounted)
│   └── overrides/             file(s) overlaid onto the stock dashboard image — see Patch 4
├── dashboard-src/             reference clone of dashboard source (STALE — see caution in local-patches.md)
├── lavalink/                  application.yml, plugins, logs
└── docs/
    └── local-patches.md       full changelog of deviations from upstream
```

## Common operations

**Rebuild and redeploy the bot** (after editing anything under `vocard-src/`):
```bash
cd vocard-src && docker build -t vocard-patched:latest . && cd ..
docker compose up -d vocard
```

**Apply a `settings.json` change** (config only, no image involved):
```bash
docker compose restart vocard   # settings.json has no hot-reload
```

**Apply a dashboard override change** (edit a file under `dashboard/overrides/`):
```bash
docker compose up -d vocard-dashboard
```

**Check everything's healthy:**
```bash
docker compose ps
docker logs vocard --tail 30
docker logs vocard-dashboard --tail 15
```

## Local Patches

This deployment deviates from upstream Vocard/Vocard-Dashboard in four ways:

1. **Direct URL resolution + prefetch/cache** — YouTube tracks resolve through
   a self-hosted invidious-companion instance to route around Lavalink's
   youtube-plugin bot-detection blocks, with the next track pre-resolved in
   the background so it doesn't add per-track lag.
   → [`vocard-src/docs/direct-url-resolver.md`](vocard-src/docs/direct-url-resolver.md)
2. **`updateVolume` relaxed to DJ-or-admin** (was admin-only), matching every
   other playback control.
3. **Music-controller button labels fixed** (`settings.json`'s translation
   keys didn't match `langs/EN.json`'s actual nested structure).
4. **Dashboard: spacebar no longer toggles play/pause** — deployed as a
   single-file bind-mount override on top of the stock dashboard image, not a
   custom-built image.
5. **Dashboard: loading / prefetch status indicators** — a small spinner on
   the current track while the bot is between songs, and on the next queue
   item while its direct URL is being pre-resolved in the background.
6. **Dashboard: resolved-URL cache size display** — shows the current size of
   the (tiny, self-bounded) resolve cache from Patch 1. No audio is cached
   anywhere in this deployment, so there's no size cap to control.

Full details, exact diffs, and how to reapply each one after a rebuild:
**[`docs/local-patches.md`](docs/local-patches.md)**.

## Testing

The bot has a test suite (unit + integration) covering the direct-URL-resolver
feature (Patch 1 above) — the rest of the bot has no tests yet.

```bash
# unit tests (fast, no network)
docker run --rm -v $(pwd)/vocard-src:/app -w /app vocard-patched:latest \
  bash -c "pip install -q -r requirements-dev.txt && pytest tests/unit -v"

# integration tests (hits the real invidious-companion + Lavalink containers)
# (load the secret first: set -a; . ./.env; set +a)
docker run --rm --network vocard -v $(pwd)/vocard-src:/app -w /app \
  -e INVIDIOUS_COMPANION_URL=http://invidious-companion:8282 \
  -e INVIDIOUS_COMPANION_SECRET=$INVIDIOUS_COMPANION_SECRET \
  vocard-patched:latest \
  bash -c "pip install -q -r requirements-dev.txt && pytest tests/integration -v -m integration"
```

Full guide (what's covered, why the tests are structured the way they are,
how to add more): **[`vocard-src/docs/testing.md`](vocard-src/docs/testing.md)**.

## Upstream docs

- [Vocard bot setup docs](https://docs.vocard.xyz/latest/bot/setup)
- [Vocard support Discord](https://discord.gg/wRCgB7vBQv)
