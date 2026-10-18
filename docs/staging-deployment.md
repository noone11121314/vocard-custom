# Staging deployment protocol

Lets code changes get built, tested, and played with in Discord *before*
they ever touch the production bot - so iterating on a change no longer
means repeatedly rebuilding/restarting the bot real users are listening to.

## How it works

A second bot container (`vocard-staging`, from `docker-compose.staging.yml`)
runs alongside the production `vocard` container, sharing the same Docker
network so it can reach the already-running `lavalink`, `vocard-db`, and
`invidious-companion` containers - none of those are duplicated. It connects
to the *same* `vocard-dashboard` the production bot uses: Vocard's dashboard
already supports multiple bots on one dashboard (the bot-selector dropdown
you see if you share a server with more than one Vocard instance), so the
staging bot just shows up there as a second selectable bot once it's
running and sharing a server with you - no second dashboard/OAuth app
needed for this.

What *is* separate, so staging can never affect production data or state:
- **A different Discord bot token/application** (unavoidable - Discord
  doesn't allow two processes to share one bot token).
- **A different Mongo database** (`VocardStaging`, inside the same `mongod`
  process/container - different db name, same server, zero collision).
- **A different on-disk stream cache** (`./stream_cache-staging/`, kept
  entirely separate from production's `./stream_cache/`).
- **A different image tag** (`vocard-patched:staging` vs. the production
  `vocard-patched:latest`) - rebuilding/restarting staging never touches
  the image tag production is actually running.

What's shared on purpose (safe to share - see "Why sharing is safe" below):
Lavalink, invidious-companion, and the dashboard container itself.

## One-time setup (you have to do this part)

I can't create a Discord application on your behalf - it needs your own
Discord account, interactively, in the Developer Portal. Once:

1. Go to https://discord.com/developers/applications → **New Application**.
   Name it something obvious, e.g. "Vocard Staging".
2. Under **Bot**, reset/copy the token, and enable the same privileged
   intents the production bot has (Server Members, Message Content, etc. -
   check the production application's Bot page for which ones are on).
3. Under **OAuth2 → URL Generator**, pick the `bot` + `applications.commands`
   scopes with the same permissions the production bot uses, and use the
   generated link to invite this staging bot to a test server - ideally
   the *same* server you already test the production bot in, since the
   dashboard only shows you a bot if you share a server with it.
4. Open `/root/vocard/settings.staging.json` and replace
   `PASTE_YOUR_STAGING_BOT_TOKEN_HERE` / `PASTE_YOUR_STAGING_BOT_CLIENT_ID_HERE`
   with what you got in steps 2-3. Everything else in that file is already
   filled in (it's a copy of production's `settings.json` with the DB name,
   activity label, and embed title tagged `[STAGING]` so it's never
   ambiguous in Discord which bot you're talking to).
5. Create the two empty directories the compose file bind-mounts:
   ```bash
   mkdir -p /root/vocard/stream_cache-staging /root/vocard/logs-staging
   ```

That's the entire one-time cost. Everything below is the repeatable loop.

## The loop: build → test → try it live → promote

```bash
cd /root/vocard

# 1. Build the staging image - note the :staging tag, never :latest.
docker build -t vocard-patched:staging -f vocard-src/Dockerfile vocard-src

# 2. Run the test suite against *that* image (same commands as always,
#    just the :staging tag - see docs/testing.md for the full versions).
docker run --rm -v "$PWD/vocard-src:/app" -w /app vocard-patched:staging \
  bash -c "pip install -q -r requirements-dev.txt && pytest tests/unit -q"

# 3. Bring staging up (or restart it to pick up a new build). Production
#    is completely untouched by this - different compose project, different
#    container, different image tag.
docker compose -f docker-compose.staging.yml up -d

# 4. Watch its logs while you try the change for real in Discord.
docker logs -f vocard-staging
```

In Discord: open the dashboard, use the bot selector to switch to the
`[STAGING]`-tagged bot, and use it exactly like the real one - play
something, check the cache manager, whatever the change touches. Repeat
step 1/3 as many times as you want; production keeps running the whole
time, for every real user, the entire loop.

When you're done with a session:

```bash
docker compose -f docker-compose.staging.yml down
```

(Leaves `./stream_cache-staging/`, `./logs-staging/`, and the
`VocardStaging` Mongo database in place for next time - add `-v` only if you
specifically want to wipe the staging bot's own stream cache, which has no
bearing on production's.)

### Promoting a validated change to production

Once you're happy with what staging showed you, the production cutover is
just a retag - you are *not* rebuilding under time pressure, because the
image you're about to run has already been live-tested:

```bash
docker tag vocard-patched:staging vocard-patched:latest
```

Then follow the existing restart-order rule from `docs/local-patches.md`
(Patch 8's deployment-order note) - **if a dashboard override file also
changed, restart `vocard-dashboard` first and confirm `docker inspect
--format='{{.State.Health.Status}}' vocard-dashboard` says `healthy`
*before* touching `vocard`; never the other order, never both without a
pause to check in between:**

```bash
dashboard/restart_dashboard.sh   # only if dashboard files changed; sets the JS
                                 # fingerprint (asset_v) and waits for healthy
docker compose up -d vocard
```

## Why sharing Lavalink/invidious-companion/the dashboard is safe

- **Lavalink**: player sessions are scoped per Discord guild-and-bot, not
  shared state - a bug in the staging bot's playback logic can only affect
  the staging bot's own voice connections, never production's.
- **invidious-companion**: a stateless URL-resolution proxy with no
  per-bot state at all; sharing it just means two bots' resolve requests
  go through the same proxy, same as any two guilds already do today.
- **The dashboard**: the only thing it persists per-bot is the IPC
  connection itself; a crash in the staging bot just means it drops off the
  dashboard's bot list until it reconnects - it can't take the production
  bot's own connection down with it.

## The one thing this *doesn't* cover: isolated dashboard-frontend testing

Because staging shares the production `vocard-dashboard` container, a
change to `dashboard/overrides/` (templates/JS) can't be previewed through
the staging bot alone - the dashboard itself would need restarting either
way to pick up a frontend change, and that's the one production container
this protocol doesn't duplicate.

In practice this is still low-impact: restarting *only* `vocard-dashboard`
(never `vocard` at the same time) is self-healing - the bot's IPC client
auto-reconnects within ~10s of the dashboard coming back (see
`voicelink/ipc/client.py`'s `_listen()`), so a frontend-only change costs a
brief dashboard blip for whoever's looking at it right then, not a bot
restart or any dropped voice connections. For most frontend tweaks, that's
an acceptable way to iterate directly against production.

If you want truly zero-impact frontend staging too (a second
`vocard-dashboard-staging` container), that needs its own Discord OAuth
app with its own registered redirect URI - the same one-time, you-have-to-
do-it-yourself step as the bot token above, just for OAuth instead of a bot
token. Not built here since it wasn't asked for, but a reasonable follow-up
if frontend iteration turns out to be a frequent need.
