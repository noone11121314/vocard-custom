# Changelog

Changes made on top of upstream [Vocard](https://github.com/ChocoMeow/Vocard)
and its dashboard. Newest first. Each entry names its patch number; the
technical write-up (cause, fix, files, tests) is in
[`docs/local-patches.md`](docs/local-patches.md).

## Permissions and settings
- Members with the server's DJ role are no longer refused queue edits when
  the bot's cached copy of their roles is out of date: a refusal is
  re-checked against Discord first. (32)
- `/settings` and dashboard settings changes, including the DJ role, apply
  to the running bot immediately instead of after a restart. (32)
- Queue reordering and volume are open to every member in the channel. (2, 8)

## Volume
- The speaker icon next to the volume slider mutes and unmutes the bot;
  unmuting (here or from Discord) brings back the saved volume. (42)
- A song that couldn't be cached no longer plays much louder than the
  rest: random YouTube refusals are retried, a song someone is waiting for
  ignores the failure cooldown, and anything still unmeasured gets the
  typical adjustment instead of full volume. (35)
- The volume shows as a "NN%" pill next to the slider, highlighted while
  dragging, with a speaker icon that follows the level. (27, 31)
- One shared bot volume, 0–100 everywhere: dashboard, `/volume` and the
  Discord buttons. (28)
- The dashboard volume always matches what the bot plays, including after
  a restart; every change is saved. (27)
- Per-track loudness normalization to -14 LUFS, with every cached song
  measured and boosts capped so they can't clip. (10, 26)

## Playback
- Play your own songs and playlists from Cantus Flaccus: paste the connect
  code Cantus gives you (server and token in one) once in the dashboard
  (Settings -> Cantus), then
  `/cantus play` (search as you type) and `/cantus playlist`, or browse
  your songs and playlists in the dashboard's new Cantus tab. Songs show
  their real title, artist and cover, keep working after restarts, and are
  evened out in volume like the rest. Your Cantus songs stay yours: others
  only see them while they're queued (and can still move or skip them),
  never in history, and can't re-queue or save them. (46)
- The bot leaves about 10 seconds after it's left alone in its voice
  channel (adjustable: `timer_settings.alone_leave`); 24/7 mode pauses
  instead. (44)
- Far fewer songs are streamed instead of cached: yt-dlp now has the
  JavaScript runtime YouTube requires, and when YouTube's bot check blocks
  yt-dlp the song is fetched through invidious-companion and cached anyway.
  (36)
- New songs are cached gently: background downloads are capped at about
  1 MB/s and run one at a time, the song someone is waiting for gets a
  faster lane, and downloads plus loudness analysis run at low CPU and disk
  priority. Loudness analysis is about 4.5× faster. (33)
- Pause and seek take effect in well under a second (Lavalink send buffer
  back to its 400ms default). (25)
- Every song, even the first, is fully downloaded before it plays, so
  nothing stutters. (1, 11)
- The queue survives a bot restart: the bot rejoins, resumes the same song
  at the same position, and keeps the paused state. (18, 20, 23, 25)

## Dashboard
- New **Full history** page (menu): every song you played or added, in any
  server, and every song played in this server - each song once, with how
  many times it was played, grouped by day, with "Load more" to go further
  back. Click a song to queue it. Old history
  was imported from the bot's and Lavalink's logs (server history back to
  24 Sep; personal history before 18 Oct only partly). (45)
- Home shows your own lists first - "Recently added by you" and "Recently
  played by you", covering every server - then the server's lists,
  labelled with its name. All lists are newest-first. (44)
- The dashboard works in several tabs or on several devices at once; they
  used to kick each other off in an endless reload loop with a red error
  bar, and a background tab went blank. "Rate limited" no longer takes
  over the page. (43)
- A Shuffle button in the queue panel, next to "Clear queue"; shuffling 3
  queued songs before pressing play works too. (41)
- Clicking a song in History adds it to the end of the queue ("Back to" is
  still in its menu), and "Add to queue" now appears in every song menu.
  With duplicates turned off, songs that already played can still be
  queued again. (40)
- The song that's playing now also shows at the top of the History list,
  marked "Now playing". (39)
- The queue's History button works on a new server (shows "No songs played
  yet" instead of doing nothing), shows the played songs after the queue
  finishes, and no longer closes itself on the next update. (38)
- Remove/move hit the right copy of a song that's in the queue twice, and
  dashboards stay in sync after the queue has finished, after skipping a
  paused song, and with multi-song "play next". Found by a new randomized
  two-dashboard sync test. (34)
- Pages reload themselves when the connection drops, when the bot
  restarts, or after 10+ minutes away, instead of going half-empty. (29)
- Dashboard JS updates go live without restarting the dashboard
  (`dashboard/apply_js.sh`). (31)
- Multiple people's dashboards stay in sync: the loading song no longer
  shows twice, shuffle no longer scrambles other dashboards, and remove/move
  always hit the right song. Covered by a two-dashboard sync test. (19, 23, 26)
- Every playback button responds instantly. (14, 16)
- Caching progress is shown on queue cards and in Now Playing, updated
  without disturbing drag-to-reorder. (5, 13, 15, 22)
- Pasted links search immediately. (24)
- Home: "Recently played" (server-wide), "Recently added" and "Recently
  played by you"; Explore uses the server's own most-played songs. (9, 17, 21)
- Audio cache manager and cache size display. (6, 7)
- Spacebar no longer toggles play/pause while typing; controller button
  labels fixed. (3, 4)

## Reliability and operations
- Every song's route is logged (`[CacheRoute]`), production bot logs are
  kept across redeploys (`./logs`), and `./cache_report.sh` shows per day
  how many songs were cached vs streamed and which route cached them. (37)
- `./run_tests.sh` runs every suite: unit, real-component integration
  tests (ffmpeg, MongoDB, Lavalink, websocket, restart restore), dashboard
  sync (scripted and randomized) and a real-Chromium page check. (34)
- The bot keeps retrying its dashboard connection until it's back. (29)
- Fixed silently dropped dashboard requests for members the bot hadn't
  cached yet. (12)
- Secrets live in ignored files and `.env`; redacted `*.example.*`
  templates are committed, and the Docker image no longer contains the bot
  token. (30)
- Staging bot alongside production; see
  [`docs/staging-deployment.md`](docs/staging-deployment.md).
