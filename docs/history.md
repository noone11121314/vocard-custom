# Project history

How this Vocard deployment got from a stock install to what it is now, day
by day. Reconstructed from the working sessions (the git history only starts
on 18 Oct, when everything up to then went in as one initial commit).
Patch numbers point to [`local-patches.md`](local-patches.md); release
notes and commits are on the
[releases page](https://github.com/noone11121314/vocard-custom/releases);
[`CHANGELOG.md`](../CHANGELOG.md) groups the same changes by topic.

## September

**9 Sep - it plays nothing.** Starting point: the stock Vocard v2.7.3 Docker
install (bot, dashboard, Lavalink, MongoDB). Songs queued, then ~10 seconds of
silence and a reset: YouTube was blocking Lavalink (bot detection, SABR
streaming). Tried a YouTube OAuth login, account cookies, PO tokens and a
newer Lavalink - none held. Found what works: an `invidious-companion`
container resolves a direct audio URL, and Lavalink plays that as a plain
HTTP stream. The dashboard's volume was admin-only - opened to DJs (2) - and
a "Unknown title / no cover" bug from the first version of the fix was
corrected.

**14 Sep - the direct-URL player.** Built that approach into the bot's
player properly (the start of Patch 1). The Discord controller's buttons showed "not found" instead of their labels (3).

**27 Sep - lag and dropouts.** Playback stuttered and sometimes stopped after
a song. Added prefetching of the next song, a resolved-URL cache and cleanup
of old entries (1); the spacebar no longer pauses while typing (4); loading
and prefetch indicators on the dashboard (5) and the cache size (6). First
unit and integration tests.

## October

**1 Oct - "only DJ can do this".** A DJ was refused queue moves on the
dashboard; tracked down how Vocard checks the DJ role.

**4 Oct - streaming stutter.** A "prewarm" of each stream, then a local
caching proxy in front of Lavalink, so the start of a song doesn't stall on
YouTube's servers.

**8 Oct - still laggy.** Checked that redeploys really applied the new code;
fixed a regression in the prefetch path and mid-stream retries.

**11 Oct - download to disk.** Replaced "stream and hope" with yt-dlp
downloading every song to a local cache before it plays, invidious-companion
as the fallback, the whole queue prefetched in order, a 5 GB cache that keeps
songs for next time, ticks and progress bars per queued song, and an audio
cache manager for DJs (7).

**12 Oct - friends can play too.** Friends without the DJ role saw nothing on
the dashboard: queue reordering and volume opened to everyone in the channel
(8), and the "Can't find you on Discord" error for members the bot hadn't
cached yet (12).

**18 Oct - the big day.** Home and Explore built from what was really played
in each server (9); loudness evened out per song (10); every song, even the
first, fully cached before it plays (11); caching shown in Now playing (13);
instant buttons (14, 16); "Recently added" (17); the queue survives a restart
(18, 20, 23, 25); a staging bot next to production; personal "Recently played
by you" (21); sync between several dashboards (22, 26); one shared 0-100
volume shown on the slider (27, 28, 31); self-reloading pages (29); secrets
kept out of git and the image, and the project put on GitHub (30); the DJ-role
fix and settings that apply live (32). **Release v0.1.0.** Then gentler
caching (33), integration / fuzz / real-browser test suites and the bugs they
found (34, 35), why some songs wouldn't download (36), cache route logging
(37) - **v0.2.0** - and the queue panel: History fixes (38-40), Shuffle (41),
the speaker icon mutes (42) - **v0.3.0**.

**19 Oct - two tabs.** Two tabs (or PC + phone) kicked each other off in a
reload loop; the dashboard now runs in several at once, and rate limits stay
quiet (43).

**20 Oct - history and Cantus.** The bot leaves ~10 s after it's left alone;
history follows each person across servers, shown first (44) - **v0.4.0**.
Full history: every song played or added, once each, rebuilt from the logs
back to 24 Sep (45) - **v0.5.0**. Cantus Flaccus (issue #1): play your own
lossless library through Vocard (46). It went through pairing and an admin
app key before settling on personal tokens and private libraries, plus a
Cantus tab in the dashboard - **v0.6.0**.

**21 Oct - one code.** Connect to Cantus with a single connect code instead
of two fields (issue #2); a Cantus tag on songs, recommendations for Cantus
songs, and no more login autofill on the search boxes (47) - **v0.7.0**.
Release tags and this history added. Later, after a host reboot,
the dashboard said "There are no nodes available": the bot had started a few
seconds before Lavalink, tried once and given up. It now keeps trying until
Lavalink is up (48, issue #3) - **v0.7.1**. The voice channel status stayed empty for
songs with an apostrophe in the title, such as "I'm" (49, issue #4) -
**v0.7.2**.
