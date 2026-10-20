# Vocard Deployment

A self-hosted [Vocard](https://github.com/ChocoMeow/Vocard) Discord music bot
stack, running via Docker Compose with a locally patched bot image and a
few dashboard file overrides. **This is a patched deployment, not vanilla
upstream** — see [Local Patches](#local-patches) before assuming stock
behavior or rebuilding anything from a clean upstream clone.

## Services

| Service | Image | Notes |
|---|---|---|
| `vocard` | `vocard-patched:latest` (built locally from `vocard-src/`) | the bot itself |
| `vocard-staging` | `vocard-patched:staging` (`docker-compose.staging.yml`) | second bot for trying changes; same image as production |
| `vocard-dashboard` | `ghcr.io/chocomeow/vocard-dashboard:latest` (stock + bind-mounted overrides) | web dashboard, port 8000 |
| `lavalink` | `ghcr.io/lavalink-devs/lavalink:latest` | audio node |
| `invidious-companion` | `quay.io/invidious/invidious-companion:latest` | direct YouTube links; the download fallback when YouTube blocks yt-dlp |
| `vocard-db` | `mongo:8` | bot + dashboard persistent storage |

See `docker-compose.yml` for the full wiring (env vars, volumes, health checks).

## Layout

```
vocard/
├── docker-compose.yml / docker-compose.staging.yml
├── .env                       compose secrets (ignored; template: .env.example)
├── settings.json              live bot config (ignored; template: settings.example.json)
├── run_tests.sh               every test suite + summary
├── deploy_when_idle.sh        deploy the built image once nothing is playing
├── cache_report.sh            cached vs streamed songs per day
├── vocard-src/                bot source -> vocard-patched image (docs/, tests/)
├── dashboard/
│   ├── overrides/             JS, templates and objects.py bind-mounted over the stock dashboard
│   ├── tests/                 sync harness, fuzz, reload and real-browser checks
│   ├── apply_js.sh            ship JS changes with no restart
│   └── restart_dashboard.sh   restart for template/compose changes
├── lavalink/                  application.yml (ignored; template committed), logs
├── docs/                      local-patches.md (every patch), staging-deployment.md
├── CHANGELOG.md               plain-language summary of the patches
└── (ignored) stream_cache/ mongodb_data/ logs/ last-session*.json dashboard-src/
```

## Common operations

Secrets live in ignored files (`settings*.json`, `dashboard/settings.json`,
`lavalink/application.yml`, `lavalink/cookies.txt`, `.env`); copy the
committed `*.example.*` / `.env.example` templates to set up a new machine.

**Deploy a bot change** (anything under `vocard-src/`): build once, tag both
bots, then let the deploy wait until no one is listening so music is never
cut:
```bash
docker build -t vocard-patched:latest -f vocard-src/Dockerfile vocard-src
docker tag vocard-patched:latest vocard-patched:staging
./deploy_when_idle.sh        # staging, then production, once nothing is playing
```
Restarts are safe: the queue is saved on shutdown and resumed (same song,
same position). See [`docs/staging-deployment.md`](docs/staging-deployment.md).

**Dashboard changes:**
```bash
dashboard/apply_js.sh          # JS only - goes live with no restart
dashboard/restart_dashboard.sh # templates (.html) or compose changes
```

**Settings** (`/settings` or the dashboard) apply to the running bot
immediately. Editing `settings.json` by hand still needs a bot restart.

**Check health and how songs are being served:**
```bash
docker compose ps
docker logs vocard --tail 30          # also kept in ./logs (30 days)
./cache_report.sh                     # per day: played from cache vs streamed, and which route cached them
grep "\[CacheRoute\]" logs/vocard.log  # every download / fallback / stream decision
```

## Local Patches

37 changes on top of upstream Vocard and its dashboard. Highlights: every
song is downloaded to a local cache before it plays (with an
invidious-companion fallback when YouTube blocks yt-dlp), per-song loudness
normalization, a shared 0-100 volume, dashboards kept in sync between
everyone in the channel, instant controls, queue survives restarts,
self-reloading dashboard pages, and no secrets in git or the image.

- What changed, in plain language: **[`CHANGELOG.md`](CHANGELOG.md)**
- Cause, fix, files and tests for each patch: **[`docs/local-patches.md`](docs/local-patches.md)**

## Cantus Flaccus

Members can play their own songs and playlists from a Cantus Flaccus server
(`/cantus play`, `/cantus playlist`). Each member connects once in the
dashboard: **Settings -> Cantus**, paste a personal token (Cantus -> Account
-> Connect other apps -> Create token, starts with `cfa_`) and Save. The
server address is a default the bot's owner, a DJ or a server admin saves
there once (or `settings.json` -> `"cantus": {"url": ..., "dashboard_url":
...}`; only public addresses). `/cantus link` explains the steps,
`/cantus unlink` forgets the token, revoking it in Cantus cuts Vocard off.
The dashboard's **Cantus** tab lists your songs (searchable) and playlists;
click a song to queue it, or "Play all" on a playlist.

A Cantus library is private: others see someone's Cantus songs only while
they're queued or playing (they can still move, skip or remove them), never
in server history or top lists, and only the owner can queue them again or
save them. Songs stream straight from Cantus (not cached). Details: Patch 46
in [`docs/local-patches.md`](docs/local-patches.md).

## Testing

```bash
./run_tests.sh               # everything, with a PASS/FAIL summary
./run_tests.sh --quick       # unit tests + scripted dashboard sync
./run_tests.sh --no-youtube  # skip the tests that download from YouTube
```

Unit tests, real-component integration tests (ffmpeg, MongoDB, Lavalink,
the dashboard websocket, restart restore), a scripted and a randomized
two-dashboard sync test, and a real-Chromium check of the minified dashboard
JS. Full guide: **[`vocard-src/docs/testing.md`](vocard-src/docs/testing.md)**.

## Upstream docs

- [Vocard bot setup docs](https://docs.vocard.xyz/latest/bot/setup)
- [Vocard support Discord](https://discord.gg/wRCgB7vBQv)
