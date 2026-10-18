const EFFECT_ICONS = {
    none: {
        text: localeTexts.effects.tags.none,
        icon: "block",
    },
    karaoke: {
        text: localeTexts.effects.tags.karaoke,
        icon: "mic_external_on",
    },
    tremolo: {
        text: localeTexts.effects.tags.tremolo,
        icon: "earthquake",
    },
    vibrato: {
        text: localeTexts.effects.tags.vibrato,
        icon: "vibration",
    },
    rotation: {
        text: localeTexts.effects.tags.rotation,
        icon: "360",
    },
    distortion: {
        text: localeTexts.effects.tags.distortion,
        icon: "adaptive_audio_mic",
    },
    lowpass: {
        text: localeTexts.effects.tags.lowpass,
        icon: "adaptive_audio_mic",
    },
    nightcore: {
        text: localeTexts.effects.tags.nightcore,
        icon: "dark_mode",
    },
    "8d": {
        text: localeTexts.effects.tags.eightD,
        icon: "360",
    },
    vaporwave: {
        text: localeTexts.effects.tags.vaporwave,
        icon: "block",
    },
};

class Timer {
    constructor(callback, interval) {
        this.callback = callback;
        this.interval = interval;
        this.timerId = null;
        this.isRunning = false;
    }

    start() {
        if (!this.isRunning) {
            this.isRunning = true;
            this.timerId = setInterval(() => {
                this.callback();
            }, this.interval);
        }
    }

    stop() {
        if (this.isRunning) {
            clearInterval(this.timerId);
            this.timerId = null;
            this.isRunning = false;
        }
    }

    getIsRunning() {
        return this.isRunning;
    }
}

class ToastManager {
    constructor(player) {
        this.player = player;
        this.queue = [];
        this.isShowing = false;
        this.fancyAlerts = {
            show: function (options) {
                var $alert = $(`
                    <div class="alert-msg ${options.type}">
                        <div class="">
                            <img class="alert-msg--icon" src="${options.avatarUrl}">
                            <div class="alert-msg--content">
                                <p class="alert-msg--words">${options.msg}</p>
                            </div>
                        </div>
                    </div>
                `);

                $(".control-container").prepend($alert);
                setTimeout(function () {
                    $alert.addClass("alert-msg__active");
                }, 100);

                setTimeout(function () {
                    $alert.addClass("alert-msg__extended");
                }, 500);

                if (options.timeout) {
                    this.hide(options.timeout);
                }

                $alert.on("fancyAlertClosed", function () {
                    options.onClose();
                });
            },
            hide: function (_delay) {
                var delay = _delay || 0;

                var $alert = $(".alert-msg");
                setTimeout(function () {
                    setTimeout(function () {
                        $alert.removeClass("alert-msg__extended");
                    }, 10);

                    setTimeout(function () {
                        $alert.removeClass("alert-msg__active");
                    }, 500);

                    setTimeout(function () {
                        $alert.trigger("fancyAlertClosed");
                        $alert.remove();
                    }, 700);
                }, delay);
            },
        };
    }

    showToast(userId, msg) {
        if (userId == undefined) return;
        this.queue.push({ userId, msg });
        if (!this.isShowing) {
            this.processQueue();
        }
    }

    processQueue() {
        if (this.queue.length === 0 || this.isShowing) {
            return;
        }
        this.isShowing = true;
        const { userId, msg } = this.queue.shift();

        let avatarUrl;
        let type;
        if (["info", "error", "success"].includes(userId)) {
            avatarUrl = `/static/img/${userId}.png`;
            type = userId;
        } else {
            avatarUrl = this.player.users[userId]?.avatarUrl || '/static/img/notFound.png';
            type = "success";
        }

        this.fancyAlerts.show({
            type: type,
            msg: msg,
            avatarUrl: avatarUrl,
            timeout: this.queue.length == 0 ? 5000 : 2000,
            onClose: () => {
                this.isShowing = false;
                this.processQueue();
            },
        });
    }
}

class Track {
    constructor(object, requester) {
        this.title = object.title;
        this.author = object.author;
        this.source = object.source;
        this.identifier = object.identifier;
        if (object.artworkUrl == undefined) {
            if (this.source == "youtube") {
                this.artworkUrl = `https://img.youtube.com/vi/${this.identifier}/hqdefault.jpg`;
            } else {
                this.artworkUrl =
                    "https://cdn.discordapp.com/attachments/674788144931012638/823086668445384704/eq-dribbble.gif";
            }
        } else {
            this.artworkUrl = object.artworkUrl;
        }
        this.isStream = object.isStream;
        this.length = Number(object.length);
        this.trackId = object.trackId;
        this.uri = object.uri;
        this.requester = requester;
    }
}

class Bot {
    constructor(params) {
        this.avatar = params.botAvatar;
        this.name = params.botName;
        this.id = params.botId.toString();
    }
}

const methods = {
    // The bot left the dashboard (restart/redeploy). Patch 29: the dashboard
    // server never re-attaches already-open pages when the bot comes back,
    // so they used to sit half-empty until a manual refresh. Keep asking for
    // the bot every few seconds (requests to an offline bot are silently
    // dropped) and reload the whole page the moment it answers.
    closeConnection: function (player, data) {
        const lostBotId = player.selectedBot?.id || localStorage.getItem("selectedBot");
        updateWarningBar(true);
        changePage("bot-not-found");
        player.updateSelectedBot(null);
        player.waitForBotToReturn(lostBotId);
    },

    rateLimited: function (player, data) {
        changePage("rate-limited");
    },

    botNotFound: function (player, data) {
        changePage("bot-not-found");
    },

    initBot: function (player, data) {
        const bot = new Bot(data);
        if (!player.bots.has(bot.id)) {
            player.bots.set(bot.id, bot);
        }
        let selectedBotId = localStorage.getItem("selectedBot");
        if (
            player.selectedBot == null &&
            (selectedBotId == undefined || bot.id == selectedBotId)
        ) {
            player.updateSelectedBot(bot.id);
        }
        player.updateSelectedBotView();
    },

    initUser: function (player, data) {
        player.userId = data.userId;

        // Patch 9: "Recently played"/"Trending" used to come from this one
        // user's own 25-track history cap (or, on Explore, a hardcoded demo
        // playlist). They're now built from this server's real, shared
        // play history instead - see getGuildHistory in methods.py. That's
        // a separate async response (handled below in methods.getGuildHistory),
        // so request it here rather than reading it synchronously off `data`.
        // guildId is best-effort (undefined until initPlayer has run at
        // least once) - the backend falls back to scanning the user's other
        // mutual guilds if it's missing.
        player.send({
            op: "getGuildHistory",
            guildId: player.guildId || undefined,
        });

        // Patch 17: "Recently added" - every track the moment it's queued,
        // regardless of whether it ever actually played. A separate async
        // response from getGuildHistory above, handled in
        // methods.getGuildAddedHistory below.
        player.send({
            op: "getGuildAddedHistory",
            guildId: player.guildId || undefined,
        });

        // A separate, personal "Recently played by you" log, as opposed to
        // getGuildHistory above (guild-wide, every member's plays). No
        // guildId needed - it's keyed purely by the viewing user's own id.
        player.send({ op: "getUserHistory" });

        player.playlists = data.data.playlist;
        player.inboxes = data.data.inbox;
        player.updatePlaylistSelector();
        player.updateInboxList();
    },

    // Patch 9: populates Home's "Recently played" (bot-searched tracks) and
    // "Recently uploaded" (tracks played from a Discord message attachment)
    // sections from this server's shared play history. The "uploaded"
    // section is hidden entirely when nobody's ever played an attachment
    // here, rather than showing an empty header.
    getGuildHistory: function (player, data) {
        const renderUnique = (containerId, trackIds) => {
            const container = $(`#${containerId}`);
            container.empty();
            const seenUris = new Set();
            [...new Set(trackIds || [])].reverse().forEach((trackId) => {
                let track;
                try {
                    track = decode(trackId);
                } catch (error) {
                    console.error(`Failed to decode track id ${trackId}: ${error}`);
                    return;
                }
                if (seenUris.has(track.uri)) return;
                seenUris.add(track.uri);
                container.append(buildTrackCardHtml(track));
            });
        };

        const botHistory = data.botHistory || [];
        const uploadHistory = data.uploadHistory || [];

        renderUnique("history-tracks-bot", botHistory);
        renderUnique("history-tracks-upload", uploadHistory);
        $("#history-tracks-upload-section").toggle(uploadHistory.length > 0);

        if (!botHistory.length && !uploadHistory.length) {
            changePage("no-history-found");
        } else {
            changePage("main-page");
        }

        var elements = $(
            "#history-tracks-bot, #history-tracks-upload, #recommendation-tracks"
        ).find("[data-id]");
        var loader = $("#recommendation-tracks-loader");
        if (loader.css("display") !== "none" && elements.length > 0) {
            var randomElement = elements.eq(
                Math.floor(Math.random() * elements.length)
            );
            player.send({
                op: "getRecommendation",
                trackId: randomElement.data("id"),
                callback: "main-page",
            });
        }
    },

    // Patch 17: populates Home's "Recently added" section - every track
    // the moment it's queued (regardless of whether it ever actually
    // played), as opposed to getGuildHistory above which only logs a
    // track once it starts playing.
    getGuildAddedHistory: function (player, data) {
        const container = $("#added-history-tracks");
        container.empty();
        const seenUris = new Set();
        [...new Set(data.addedHistory || [])].reverse().forEach((trackId) => {
            let track;
            try {
                track = decode(trackId);
            } catch (error) {
                console.error(`Failed to decode track id ${trackId}: ${error}`);
                return;
            }
            if (seenUris.has(track.uri)) return;
            seenUris.add(track.uri);
            container.append(buildTrackCardHtml(track));
        });
    },

    // Populates Home's "Recently played by you" section - this one user's
    // own play history (getUserHistory in methods.py), separate from
    // getGuildHistory above which is shared/guild-wide across all members.
    getUserHistory: function (player, data) {
        const container = $("#user-history-tracks");
        container.empty();
        const seenUris = new Set();
        [...new Set(data.history || [])].reverse().forEach((trackId) => {
            let track;
            try {
                track = decode(trackId);
            } catch (error) {
                console.error(`Failed to decode track id ${trackId}: ${error}`);
                return;
            }
            if (seenUris.has(track.uri)) return;
            seenUris.add(track.uri);
            container.append(buildTrackCardHtml(track));
        });
        $("#user-history-section").toggle(container.children().length > 0);
    },

    // Patch 9: populates Explore's "Trending Songs" (bot-searched) and "Top
    // Uploads" (Discord-attachment) sections from this server's own
    // most-played tracks (see getGuildTopTracks in methods.py), replacing
    // the old hardcoded single YouTube Music playlist.
    getGuildTopTracks: function (player, data) {
        const renderInto = (containerId, trackIds) => {
            const container = $(`#${containerId}`);
            container.empty();
            (trackIds || []).forEach((trackId) => {
                try {
                    container.append(buildTrackCardHtml(decode(trackId)));
                } catch (error) {
                    console.error(`Failed to decode track id ${trackId}: ${error}`);
                }
            });
        };

        renderInto("top-tracks-bot", data.botTracks);
        renderInto("top-tracks-upload", data.uploadTracks);
        $("#top-tracks-upload-section").toggle((data.uploadTracks || []).length > 0);
    },

    initPlayer: function (player, data) {
        player.init();
        player.guildId = data.guildId;
        // Fresh connect/reconnect (page refresh, guild switch) - pull in
        // whatever's already cached/downloading right now instead of
        // leaving every tick/progress bar blank until the next change-only
        // prefetchStatus/cacheStats push happens to fire. Overwrites, not
        // merges: this is the authoritative state as of right now.
        player.prefetchStatuses = data.prefetchStatuses || {};
        player.cacheStats = data.cacheStats || { entries: 0, approxBytes: 0 };
        data["users"].forEach((user) => {
            player.addUser(user);
        });
        player.queue.push(
            ...(data.tracks
                .map((track) => {
                    try {
                        return decode(
                            track?.trackId,
                            player.users[track?.requesterId]
                        );
                    } catch (error) {
                        console.error(
                            `Failed to decode track id ${track?.trackId}: ${error}`
                        );
                        return null;
                    }
                })
                .filter((track) => track !== null) || [])
        );
        player.isDJ = data?.isDj;
        // Patch 23: init() above resets hasActiveTrack to false, and nothing
        // here used to set it back - so after every page load/reconnect
        // (e.g. returning to a backgrounded tab) the queue list started at
        // the current track instead of after it, showing the playing song in
        // both Now Playing and "Next in queue" and shifting every card's
        // index by one (so removing the "duplicate" removed the next song).
        // Must be set before updateCurrentQueuePos(), which re-renders.
        player.hasActiveTrack = !!data?.hasCurrentTrack;
        player.loadingTrackId = data?.loadingTrackId || null;
        player.isTrackLoading = !!data?.loadingTrackId;
        player.updateCurrentQueuePos(data?.currentQueuePosition);
        player.updateNowPlayingPrefetchBar();
        player.isPaused = data?.isPaused;
        player.currentPosition = data?.currentPosition;
        player.repeat = data?.repeatMode;
        player.channelName = data?.channelName;
        player.autoplay = data?.autoplay;
        if (typeof data?.volume === "number") player.volume = data.volume;
        player.updateVolumeView(data?.volume);
        // Volume is open to every member in the channel, not just the DJ -
        // see local-patches.md's Patch 8 (matches updateVolume no longer
        // requiring player.is_privileged() on the bot side).
        player.volumeBar.removeAttr("disabled");
        player.updateChannelMemberView();

        player.availableFilters = data?.availableFilters;
        player.filters = data?.filters;
        player.updateFilterView();
        player.updateCacheStatsView();

        $("#queue").sortable({
            animation: 150,
            ghostClass: "sortable-ghost",
            chosenClass: "sortable-chosen",
            // Queue reordering is open to every member in the channel, not
            // just the DJ - see local-patches.md's Patch 8 (matches
            // moveTrack no longer requiring player.is_privileged() on the
            // bot side).
            disabled: false,

            onEnd: function (evt) {
                let index = evt.oldIndex + 1;
                let newIndex = evt.newIndex + 1;
                if (index != newIndex) {
                    player.send({
                        op: "moveTrack",
                        index: evt.oldIndex + 1,
                        newIndex: evt.newIndex + 1,
                    });
                }
            },
        });
    },

    getRecommendation: function (player, data) {
        const region = $(`#${data.callback}`);
        const recommendationTrack = region.find(".recommendation-tracks");
        region.find(".loader").fadeOut(150);

        const existingTrackIds = new Set(
            recommendationTrack
                .find("[data-id]")
                .map(function () {
                    return $(this).data("id");
                })
                .get()
        );

        data.tracks.forEach((trackId) => {
            // Skip if the track is already in the recommendation list
            if (existingTrackIds.has(trackId)) return;

            const track = decode(trackId);

            // Append the new track
            recommendationTrack.append(buildTrackCardHtml(track));
        });
    },

    getLyrics: function (player, data) {
        if (Object.keys(data.lyrics).length === 0) {
            changePage("no-lyrics-found");
        } else {
            pageId = data.callback;
            $(`#${pageId}`).replaceWith(buildLyricHtml(pageId, data));
        }
    },

    playerUpdate: function (player, data) {
        player.lastUpdate = data["lastUpdate"];
        player.isConnected = data["isConnected"];
        player.currentPosition = data["lastPosition"];
    },

    trackUpdate: function (player, data) {
        player.isTrackLoading = false;
        player.loadingTrackId = null;
        player.hasActiveTrack = !!data["trackId"];
        let track = player.updateCurrentQueuePos(data["currentQueuePosition"]);
        player.isPaused = data["isPaused"];
        player.updateNowPlayingPrefetchBar();
        if (track?.trackId != data["trackId"]) {
            player.send({ op: "initPlayer" });
        }
    },

    // Sent right before the bot resolves/hands off the next track to Lavalink,
    // ahead of the trackUpdate that follows once it's actually loaded - lets the
    // UI show a loading indicator for the (usually short, but since Patch 11
    // can now mean "downloading the whole song first" for a cache miss) gap
    // between songs. Patch 13: also eagerly shows this track's title/artwork
    // in the Now Playing panel right away (rather than leaving the previous
    // track's info sitting there until trackUpdate finally arrives), and
    // remembers trackId so a following prefetchStatus for it can drive the
    // Now Playing caching/progress bar.
    trackLoading: function (player, data) {
        player.isTrackLoading = true;
        player.loadingTrackId = data?.trackId || null;
        // This is the moment a track actually becomes "current" from the
        // dashboard's point of view (see Patch 15's note on hasActiveTrack) -
        // re-render the queue list now so whichever card this is moves out
        // of "queue" and into Now Playing exactly when loading starts, not
        // a moment earlier (addTrack) or later (trackUpdate).
        player.hasActiveTrack = true;

        // Patch 26: the bot now sends the new queue position with this
        // event too. Adopting it here (instead of only on trackUpdate) keeps
        // every dashboard's indexes in line with the bot for the whole
        // caching window - before, the loading song also showed as the
        // first "Next in queue" card and remove/move/skip-to were off by one.
        if (data?.currentQueuePosition !== undefined) {
            const track = player.updateCurrentQueuePos(data.currentQueuePosition);
            player.updateNowPlayingPrefetchBar();
            if (data?.trackId && track?.trackId !== data.trackId) {
                player.send({ op: "initPlayer" });
            }
            return;
        }

        if (data?.trackId) {
            try {
                const track = decode(data.trackId);
                player.updateImage("#now-playing-img", track.artworkUrl);
                $("#now-playing-title").text(track.title);
                $("#now-playing-description").text(track.author);
                $("#now-playing-section").show();
            } catch (error) {
                console.error(`Failed to decode loading track id ${data.trackId}: ${error}`);
            }
        }

        player.updateQueueList();
        player.updateNowPlayingPrefetchBar();
    },

    // Progress of a background/on-demand resolve for either an upcoming
    // queue track (prefetch, while a previous one plays) or - Patch 13 -
    // the track that's actually loading right now (see trackLoading above
    // and Player._resolve_and_notify_current_track). "loading" shows a
    // progress bar under the track name (data.progress, 0-1, while known);
    // "ready" swaps that for a small checkmark (fully cached, starts
    // instantly); "unavailable" just clears it - nothing to show, no
    // second chance short of the next prefetch pass.
    prefetchStatus: function (player, data) {
        if (data.status === "loading") {
            player.prefetchStatuses[data.trackId] = { status: "loading", progress: data.progress };
        } else if (data.status === "ready") {
            player.prefetchStatuses[data.trackId] = { status: "ready" };
        } else {
            delete player.prefetchStatuses[data.trackId];
        }
        // Patches progress in place rather than a full updateQueueList() -
        // this can fire many times a second while a track downloads, and a
        // full $("#queue").html(...) rebuild on every tick was tearing down
        // and recreating the very DOM elements Sortable.js tracks for
        // drag-to-reorder, making the list look like it kept "refreshing"
        // out from under a drag. See updateQueueTrackIndicator below.
        player.updateQueueTrackIndicator(data.trackId);
        player.updateNowPlayingPrefetchBar();
    },

    // Size of the bot's in-memory resolved-URL cache for upcoming YouTube
    // tracks (metadata/URL strings, not audio - always tiny, no size cap
    // needed). Purely informational.
    cacheStats: function (player, data) {
        player.cacheStats = { entries: data.entries, approxBytes: data.approxBytes };
        player.updateCacheStatsView();
    },

    // Response to both getCacheEntries (opening the cache manager) and
    // deleteCacheEntry (after a delete) - the yt-dlp disk cache's full
    // entry list, size vs. STREAM_CACHE_MAX_BYTES, from the dashboard's
    // cache-manager modal (see action.js's buildCacheManagerModalHtml).
    // Re-renders the modal in place if it's already open, so a delete just
    // updates the list it's already looking at instead of flicker-closing
    // and reopening it.
    cacheEntries: function (player, data) {
        const $existing = $('.modal-background[data-id="cache-manager"]');
        if ($existing.length) {
            $existing.replaceWith(buildCacheManagerModalHtml(data));
        } else {
            closeAllModals();
            $("html").append(buildCacheManagerModalHtml(data));
        }
    },

    addTrack: function (player, data) {
        let tracks = data.tracks
            .map((trackId) => {
                try {
                    return decode(trackId, player.users[data?.requesterId]);
                } catch (error) {
                    console.error(
                        `Failed to decode track id ${trackId}: ${error}`
                    );
                    return null;
                }
            })
            .filter((track) => track !== null);

        if (data.tracks.length == 1) {
            var msg = formatString(
                localeTexts.addTrack,
                decode(data.tracks[0]).title
            );
        } else {
            var msg = formatString(localeTexts.addMultipleTrack, tracks.length);
        }
        player.tm.showToast(data["requesterId"], msg);

        if (data.position >= 1 && player.isPlaying()) {
            player.queue.splice(
                player.currentQueuePosition + data.position,
                0,
                ...tracks
            );
        } else {
            player.queue.push(...tracks);
        }
        // Patch 15: only re-derive "now playing" (updateCurrentQueuePos)
        // when there's already a confirmed current track. Without this
        // guard, adding the very first track(s) ever to an idle player hit
        // updateCurrentQueuePos()'s stale currentQueuePosition default (0)
        // and got misread as "now playing" the instant this addTrack event
        // arrived - hiding it from the queue list entirely instead of
        // showing it there immediately like any other newly-queued song.
        // It correctly moves into Now Playing a moment later anyway, once
        // trackLoading actually fires for it (see that handler).
        if (player.hasActiveTrack) {
            player.updateCurrentQueuePos();
        } else {
            player.updateQueueList();
        }
    },

    getTracks: function (player, data) {
        let callback = data?.callback;
        let tracks = data?.tracks;

        if (tracks == undefined) return;

        const region = $(`#${callback}`);

        if (callback == "search-result-tracks") {
            $("#result-count").text(`${localeTexts.resultsFoundCount}: ${tracks.length}`);
            region.empty();
            player.searchList = tracks;
            for (let i in tracks) {
                let track = decode(tracks[i]);
                region.append(`
                    <div class="track-row">
                        <div class="left">
                            <img src="${track.artworkUrl
                    }" onerror="this.src='/static/img/notFound.png'" alt="">
                            <div class="track-info">
                                <p class="title">${track.title}</p>
                                <p class="description">${track.author}</p>
                            </div>
                        </div>
                        <p>${track.isStream ? "LIVE" : msToReadableTime(track.length)
                    }</p>
                    </div>`);
            }
            $(".search-result").fadeIn(200);
            $("#search-loader").fadeOut(200);
            $("#search-result-tracks").animate({ scrollTop: 0 }, "slow");
        } else if (callback.startsWith("playlist-page-")) {
            selectedPlaylistPayload.tracks = tracks;
            region.replaceWith(
                buildPlaylistHtml(
                    callback.replace("playlist-page-", ""),
                    selectedPlaylistPayload,
                    "playlist"
                )
            );
        } else {
            region.empty();

            data.tracks.forEach((trackId) => {
                const track = decode(trackId);

                // Append the new track
                region.append(buildTrackCardHtml(track));
            });
        }
    },

    playerClose: function (player, data) {
        player.init();
    },

    updateGuild: function (player, data) {
        const user = data["user"];
        player.channelName = data["channelName"];

        if (data["isJoined"]) {
            player.addUser(user);
        } else {
            if (player.users.hasOwnProperty(user["userId"])) {
                delete player.users[user["userId"]];
            }
        }
        player.updateChannelMemberView();
    },

    updatePause: function (player, data) {
        player.isPaused = data["pause"];
        player.tm.showToast(
            data["requesterId"],
            player.isPaused ? localeTexts.paused : localeTexts.resumed
        );
    },

    updatePosition: function (player, data) {
        const position = msToReadableTime(data.position);
        player.tm.showToast(
            data.requesterId,
            formatString(
                data.position >= player.currentPosition
                    ? localeTexts.forward
                    : localeTexts.rewind,
                position
            )
        );
        player.currentPosition = data.position;
    },

    updateVolume: function (player, data) {
        if (typeof data?.volume === "number") player.volume = data.volume;
        player.updateVolumeView(data?.volume);
        player.tm.showToast(
            data.requesterId,
            formatString(localeTexts.volume, data?.volume)
        );
    },

    swapTrack: function (player, data) {
        const { index1: firstTrackData, index2: secondTrackData } = data;

        const firstTrackIndex =
            player.currentQueuePosition + firstTrackData.index;
        const secondTrackIndex =
            player.currentQueuePosition + secondTrackData.index;

        const firstTrack = player.queue.at(firstTrackIndex);
        const secondTrack = player.queue.at(secondTrackIndex);

        if (
            firstTrack.trackId != secondTrackData.trackId ||
            secondTrack.trackId != firstTrackData.trackId
        ) {
            return player.send({ op: "initPlayer" });
        }

        [player.queue[firstTrackIndex], player.queue[secondTrackIndex]] = [
            player.queue[secondTrackIndex],
            player.queue[firstTrackIndex],
        ];
        player.tm.showToast(
            data["requesterId"],
            formatString(
                localeTexts.swapTrack,
                firstTrack.title,
                secondTrack.title
            )
        );
        player.updateCurrentQueuePos();
    },

    moveTrack: function (player, data) {
        let movedTrack = data?.movedTrack;
        let newIndex = data?.newIndex;

        let element = player.queue.splice(
            player.currentQueuePosition + movedTrack?.index,
            1
        )[0];
        if (element?.trackId != movedTrack?.trackId) {
            return player.send({ op: "initPlayer" });
        }
        player.queue.splice(player.currentQueuePosition + newIndex, 0, element);
        player.tm.showToast(
            data["requesterId"],
            formatString(localeTexts.moveTrack, element.title, newIndex)
        );
        player.updateCurrentQueuePos();
    },

    shuffleTrack: function (player, data) {
        const tracks = data?.tracks;
        // Patch 26: the bot sends "queue_type" (snake_case), but this only
        // ever read "queueType" - so every shuffle of the upcoming queue was
        // applied as a *history* shuffle on every dashboard, writing the
        // shuffled songs over the history (songs shown twice, every index
        // off) and then throwing on capitalize(undefined).
        const queueType = data?.queueType ?? data?.queue_type;

        if (!tracks) return;

        const decodedTrack = (track) => {
            try {
                return decode(track?.trackId, player.users[track?.requesterId]);
            } catch (error) {
                console.error(
                    `Failed to decode track id ${track?.trackId}: ${error}`
                );
                return null;
            }
        };

        if (queueType === "queue") {
            player.queue.splice(player.currentQueuePosition + 1);
            player.queue.push(
                ...tracks.map(decodedTrack).filter((track) => track !== null)
            );
        } else {
            player.queue.splice(
                0,
                player.currentQueuePosition,
                ...tracks.map(decodedTrack).filter((track) => track !== null)
            );
        }

        player.tm.showToast(
            data.requesterId,
            formatString(localeTexts.shuffleTracks, capitalize(queueType))
        );
        player.updateCurrentQueuePos();
    },

    repeatTrack: function (player, data) {
        player.repeat = data.repeatMode;
        player.tm.showToast(
            data["requesterId"],
            formatString(localeTexts.repeatTrack, data.repeatMode)
        );
    },

    removeTrack: function (player, data) {
        const indexes = data.indexes;
        const firstTrackId = data.firstTrackId;

        if (player.queue.at(indexes[0]).trackId !== firstTrackId) {
            player.send({ op: "initPlayer" });
        }

        for (let i = indexes.length - 1; i >= 0; i--) {
            player.queue.splice(indexes[i], 1);
        }

        if (indexes.length == 1) {
            var msg = formatString(
                localeTexts.removeTrack,
                decode(firstTrackId).title
            );
        } else {
            var msg = formatString(localeTexts.removeMultiple, indexes.length);
        }
        player.tm.showToast(data["requesterId"], msg);
        player.updateCurrentQueuePos();
    },

    clearQueue: function (player, data) {
        let queueType = data?.queueType;
        if (queueType === "queue") {
            // Clear all tracks after the current position
            player.queue.splice(
                player.currentQueuePosition + 1,
                player.queue.length - (player.currentQueuePosition + 1)
            );
        } else if (queueType === "history") {
            // Ensure current position is valid and clear history
            if (player.currentQueuePosition > 0) {
                player.queue.splice(0, player.currentQueuePosition);
                player.currentQueuePosition = 0;
            }
        }

        player.tm.showToast(
            data["requesterId"],
            formatString(localeTexts.clearQueue, data.queueType)
        );
        player.updateCurrentQueuePos();
    },

    toggleAutoplay: function (player, data) {
        player.autoplay = data.status;
        player.tm.showToast(
            data["requesterId"],
            formatString(
                localeTexts.autoplay,
                player.autoplay ? localeTexts.enabled : localeTexts.disabled
            )
        );
    },

    loadPlaylist: function (player, data) {
        var playlist = player.playlists[data.playlistId];
        playlist.tracks = data.tracks;
        $(`#playlist-page-${data.playlistId}`).replaceWith(
            buildPlaylistHtml(data.playlistId, playlist, "user-playlist")
        );
    },

    updatePlaylist: function (player, data) {
        let status = data.status;

        if (status == "created") {
            player.playlists[data.playlistId] = data.data;
            player.updatePlaylistSelector();
            player.tm.showToast("success", data.msg);
            closeAllModals();
        } else if (status == "deleted") {
            delete player.playlists[data.playlistId];
            player.updatePlaylistSelector();
            backToLastPage();
            player.tm.showToast("success", data.msg);
            closeAllModals();
        } else if (status == "renamed") {
            player.playlists[data.playlistId].name = data.name;
            player.updatePlaylistSelector();
            player.tm.showToast("success", data.msg);
            closeAllModals();
        } else if (status == "error") {
            let $errorSection = $(
                `.modal-container .section[data-id="${data.field}"]`
            );
            $errorSection.addClass("error");
            $errorSection.find(".error-msg").text(data.msg);
        } else if (status == "addTrack") {
            if (data.playlistId in player.playlists) {
                player.playlists[data.playlistId]?.tracks.push(data.trackId);
                player.tm.showToast("success", data.msg);
            }
        } else if (status == "removeTrack") {
            if (data.playlistId in player.playlists) {
                let trackId =
                    player.playlists[data.playlistId]?.tracks[
                    data.trackPosition
                    ];
                if (trackId != data.trackId) return;

                player.playlists[data.playlistId]?.tracks.splice(
                    data.trackPosition,
                    1
                );
                $(`#playlist-page-${data.playlistId}`).replaceWith(
                    buildPlaylistHtml(
                        data.playlistId,
                        player.playlists[data.playlistId],
                        "user-playlist"
                    )
                );
                player.tm.showToast("success", data.msg);
            }
        } else if (status == "updateInbox") {
            if (data?.accept) {
                player.playlists[data.playlistId] = data.data;
                player.updatePlaylistSelector();
                player.tm.showToast("success", data.msg);
            }

            player.inboxes = player.inboxes.filter(
                (mail) =>
                    mail.sender.id !== data.senderId &&
                    mail.referId !== data.referId
            );
            player.updateInboxList();
        }

        player.updateCurrentQueuePos();
    },

    getMutualGuilds: function (player, data) {
        const $settingsPage = $("#settings-page");

        const createServerCard = (serverId, serverData, isInvite = false) => {
            return `
                <div class="server-card ${isInvite ? "" : "access-server-settings"
                }" data-id="${serverId}">
                    <div class="banner">
                        <img src="${serverData.banner
                }" onerror="this.src='/static/img/default-banner.svg'" alt="">
                    </div>
                    <div class="info">
                        <img class="server-icon" src="${serverData.avatar
                }" onerror="this.src='/static/img/notFound.png'" alt="">
                        <div class="server-info">
                            <p class="title">${serverData.name}</p>
                            <p class="description">${isInvite
                    ? localeTexts.settings.variable.inviteText
                    : `${serverData.memberCount}&nbsp;${localeTexts.settings.variable.membersText}`
                }</p>
                        </div>
                    </div>
                    <div class="action-info">
                        <span class="material-symbols-outlined">${isInvite ? "add" : "construction"
                }</span>
                    </div>
                </div>`;
        };

        let html = "";

        if (player.guildId !== null && data.mutualGuilds[player.guildId]) {
            const serverData = data.mutualGuilds[player.guildId];
            html += `
                <div class="section">
                    <div class="header">
                        <div class="sub-title">
                            <h2>${localeTexts.settings.variable.header1}</h2>
                        </div>
                    </div>
                    <div class="server-cards">
                        ${createServerCard(player.guildId, serverData)}
                    </div>
                </div>`;
        }

        // My Servers Section
        html += `
            <div class="section">
                <div class="header">
                    <div class="sub-title">
                        <p>${localeTexts.settings.variable.description2}</p>
                        <h2>${localeTexts.settings.variable.header2}</h2>
                    </div>
                </div>
                <div class="server-cards">
                    ${Object.entries(data.mutualGuilds)
                .map(([serverId, serverData]) => {
                    if (player.guildId && player.guildId === serverId)
                        return "";
                    return createServerCard(serverId, serverData);
                })
                .join("")}
                </div>
            </div>`;

        // Invite Bot Section
        html += `
            <div class="section">
                <div class="header">
                    <div class="sub-title">
                        <p>${localeTexts.settings.variable.description3}</p>
                        <h2>${localeTexts.settings.variable.header3}</h2>
                    </div>
                </div>
                <div class="server-cards">
                    ${Object.entries(data.inviteGuilds)
                .map(
                    ([serverId, serverData]) => `
                        <a href="${`https://discord.com/oauth2/authorize?client_id=${player.selectedBot.id}&permissions=2184538176&scope=bot%20applications.commands`}" target="_blank" rel="noopener noreferrer">
                            ${createServerCard(serverId, serverData, true)}
                        </a>
                    `
                )
                .join("")}
                </div>
            </div>`;

        html += getFooterHtml();
        $settingsPage.html(html);
    },

    getSettings: function (player, data) {
        player.currentSettings = data;
        $(`#server-page-${data.guild.id}`).replaceWith(
            buildSettingPageHtml(data.guild.id, {
                guild: data.guild,
                fields: {
                    prefix: {
                        title: localeTexts.settings.prefix.title,
                        description: localeTexts.settings.prefix.description,
                        placeholder: "?",
                        default: data.settings.prefix,
                        inputType: "text",
                        maxLength: 3,
                        optionClasses: [],
                    },
                    lang: {
                        title: localeTexts.settings.language.title,
                        description: localeTexts.settings.language.description,
                        default: data.settings.lang,
                        options: data.options.languages,
                        inputType: "dropdown",
                        optionClasses: [],
                    },
                    queue_type: {
                        title: localeTexts.settings.queueType.title,
                        description: localeTexts.settings.queueType.description,
                        default: data.settings.queueType,
                        options: data.options.queueModes,
                        inputType: "dropdown",
                        optionClasses: [],
                    },
                    dj: {
                        title: localeTexts.settings.dj.title,
                        description: localeTexts.settings.dj.description,
                        default: data.settings.dj,
                        options: data.options.roles,
                        inputType: "dropdown",
                        optionClasses: [],
                    },
                    "24/7": {
                        title: localeTexts.settings._247.title,
                        description: localeTexts.settings._247.description,
                        default: data.settings["24/7"],
                        inputType: "switch",
                        optionClasses: [],
                    },
                    disabled_vote: {
                        title: localeTexts.settings.voteDisable.title,
                        description:
                            localeTexts.settings.voteDisable.description,
                        default: data.settings?.disabled_vote,
                        inputType: "switch",
                        optionClasses: [],
                    },
                    controller_msg: {
                        title: localeTexts.settings.controllerMsg.title,
                        description:
                            localeTexts.settings.controllerMsg.description,
                        default: data.settings?.controller_msg,
                        inputType: "switch",
                        optionClasses: [],
                    },
                    duplicate_track: {
                        title: localeTexts.settings.duplicateTrack.title,
                        description:
                            localeTexts.settings.duplicateTrack.description,
                        default: data.settings?.duplicate_track,
                        inputType: "switch",
                        optionClasses: [],
                    },
                    silent_msg: {
                        title: localeTexts.settings.silentMsg.title,
                        description: localeTexts.settings.silentMsg.description,
                        default: data.settings?.silent_msg,
                        inputType: "switch",
                        optionClasses: [],
                    },
                    stage_announce_template: {
                        title: localeTexts.settings.stageAnnounceTemplate.title,
                        description:
                            localeTexts.settings.stageAnnounceTemplate
                                .description,
                        default: data.settings.stage_announce_template,
                        inputType: "text",
                        optionClasses: ["long-input"],
                    },
                },
            })
        );
    },

    updateFilter: function (player, data) {
        if (data?.type === "reset") {
            player.filters = [];
            player.tm.showToast(data["requesterId"], localeTexts.effects.reset);
        } else {
            let index = player.filters.findIndex(
                (filter) => filter.tag === data.filter.tag
            );
            if (data?.type === "add") {
                if (index !== -1) {
                    player.filters[index] = data.filter;
                } else {
                    player.filters.push(data.filter);
                }
                player.tm.showToast(
                    data["requesterId"],
                    formatString(localeTexts.effects.add, data.filter.tag)
                );
            } else if (data?.type == "remove") {
                if (index !== -1) {
                    player.filters.splice(index, 1);
                    player.tm.showToast(
                        data["requesterId"],
                        formatString(
                            localeTexts.effects.remove,
                            data.filter.tag
                        )
                    );
                }
            }
        }

        player.updateFilterView();
    },

    getFeaturedPlaylists: function (player, data) {
        const region = $(`#${data.callback}`);
        region.empty();

        data.playlists.forEach((playlist) => {
            // Append the new playlist
            region.append(buildPlaylistCardHtml(playlist));
        });
    },

    getCategoryPlaylists: function (player, data) {
        const region = $(`#${data.callback}`);
        region.empty();

        data.playlists.forEach((playlist) => {
            // Append the new playlist
            region.append(buildPlaylistCardHtml(playlist));
        });
    },

    errorMsg: function (player, data) {
        player.tm.showToast(data["level"], data["msg"]);
    },
};

class Player {
    constructor() {
        this.socket = new Socket(
            this,
            `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.hostname
            }:${window.location.port}/ws_user`
        );
        this.socket.connect(this);
        this.socket.addMessageListener((msg) => this.handleMessage(msg));
        this.tm = new ToastManager(this);
        this.timer = new Timer(() => this.updateTime(), 1000);
        this.userId = null;
        this.isDJ = false;
        this.date = new Date();
        this.queue = [];

        this.guildId = null;
        this.users = {};
        this.searchList = [];
        this.repeat = "off";
        this.currentTrack = null;
        this.currentQueuePosition = 0;
        this.currentPosition = 0;
        this.isPaused = false;
        this.volume = 100;
        this.lastUpdate = 0;
        this.isConnected = true;
        this.autoplay = false;
        this.isTrackLoading = false;
        this.loadingTrackId = null;
        // Patch 15: true once the backend has actually told us there's a
        // current track (trackLoading/trackUpdate) - see updateQueueList()/
        // updateCurrentQueuePos() for why this matters: currentQueuePosition
        // defaults to 0, which used to make the very first track ever added
        // to an idle player get misread as "now playing" the instant its
        // addTrack event arrived, before the backend had even started
        // resolving it - hiding it from the queue list instead of showing
        // it there immediately like any other newly-queued song.
        this.hasActiveTrack = false;
        this.prefetchStatuses = {};
        this.cacheStats = { entries: 0, approxBytes: 0 };
        this._lastAddTracksSend = null;

        this.bots = new Map();
        this.selectedBot = null;

        this.channelName = "";
        this.playlists = [];
        this.inboxes = [];

        this.currentSettings = {};
        this.modifySettings = {};

        this.filters = [];
        this.availableFilters = [];

        this.positionBar = $("#position-bar");
        this.volumeBar = $("#volume-bar");
        this.likeBtn = $("#like-btn");
        this.startTime = $("#start-time");

        this.updateFilterView();
    }

    waitForBotToReturn(botId) {
        if (!botId || this.botReturnProbe) return;
        this.waitingForBotReturn = true;
        this.botReturnProbe = setInterval(() => this.send({ op: "updateSelectedBot", botId: botId }), 3000);
    }

    handleMessage(msg) {
        const data = JSON.parse(msg);
        const op = data.op;
        if (this.waitingForBotReturn && ["initBot", "initUser", "initPlayer"].includes(op)) {
            return hardReload("the bot is back");
        }
        const validMethods = Object.keys(methods);

        if (validMethods.includes(op)) {
            methods[op](this, data);
        } else {
            console.log(`Invalid action: ${op}`);
        }

        return this.updateInfo();
    }

    init() {
        this.isDJ = false;
        this.date = new Date();
        this.queue = [];

        this.guildId = null;
        this.users = {};
        this.repeat = "off";
        this.currentTrack = null;
        this.currentQueuePosition = 0;
        this.currentPosition = 0;
        this.isPaused = false;
        this.volume = 100;
        this.lastUpdate = 0;
        this.isConnected = true;
        this.autoplay = false;
        this.isTrackLoading = false;
        this.loadingTrackId = null;
        this.hasActiveTrack = false;
        // Deliberately NOT reset here: prefetchStatuses/cacheStats (see their
        // constructor assignments for the real first-time init). init() also
        // runs for a same-guild resync (trackUpdate sends "initPlayer" when
        // its locally-cached current track doesn't match the bot's) - not
        // just a genuine guild switch/player close - and that resync should
        // not wipe prefetch ticks/progress bars for tracks still sitting in
        // the same queue a moment later. Both are keyed by a stable track id
        // that survives the resync intact, so a stale leftover entry for a
        // track that's no longer in view is simply never looked up again -
        // harmless - rather than every track's indicator flashing back to
        // nothing on every ordinary song transition.
        this.channelName = "";
        this.filters = [];

        this.updateCurrentQueuePos();
        this.updateSelectedBotView();
        this.updateChannelMemberView();
        this.updateFilterView();
        this.updateInfo();
        this.updateCacheStatsView();
    }

    addUser(user) {
        this.users[user["userId"]] = { ...user };
    }

    // Patch 16: true exactly when methods.py's own bypass condition for
    // skipTo/backTo/updatePause's internal vote-gating is true -
    // player.is_privileged(member) (mirrored here by isDJ) OR being the
    // current track's own requester. Only in that case is a given click
    // *guaranteed* to take effect immediately rather than just register
    // one vote among several needed - optimistically updating the UI for
    // a vote that might not actually pass yet would show a flatly wrong
    // state (e.g. "paused" when it's actually still 1 of 3 votes), not
    // just an imprecise one, so every optimistic update below checks this
    // first and falls back to the old wait-for-the-real-response behavior
    // otherwise.
    canActImmediately() {
        return !!this.isDJ || (!!this.currentTrack && this.currentTrack.requester?.userId === this.userId);
    }

    togglePause() {
        // Patch 14/16: flip the icon/position-timer right now instead of
        // waiting out the full round trip (browser -> dashboard -> bot ->
        // Lavalink -> bot -> dashboard -> browser) before anything visibly
        // changes - that chain is where the ~1s felt lag was coming from,
        // not the click itself. Purely an optimistic UI update, gated on
        // canActImmediately() (see above) since updatePause is vote-gated
        // for a non-DJ/non-requester - the eventual updatePause response
        // still sets the authoritative isPaused and shows the toast, so a
        // rare disagreement self-corrects instead of getting stuck wrong.
        if (this.canActImmediately()) {
            this.isPaused = !this.isPaused;
            this.updateInfo();
            this.send({ op: "updatePause", pause: this.isPaused });
        } else {
            this.send({ op: "updatePause", pause: !this.isPaused });
        }
    }

    skipTo(index = 1) {
        // Patch 16: for the common "skip to the very next track" click
        // (not a context-menu jump to an arbitrary queue position), show
        // that track's info immediately - same loading-preview treatment
        // trackLoading gives a normal track change, just started a moment
        // earlier from the click itself rather than waiting for the
        // server to tell us. Gated on canActImmediately() - skipTo is
        // vote-gated otherwise, and a vote that doesn't actually pass yet
        // must not look like it already skipped.
        if (index === 1 && this.canActImmediately()) {
            const nextTrack = this.queue[this.currentQueuePosition + 1];
            if (nextTrack) {
                this.isTrackLoading = true;
                this.loadingTrackId = nextTrack.trackId;
                this.updateImage("#now-playing-img", nextTrack.artworkUrl);
                $("#now-playing-title").text(nextTrack.title);
                $("#now-playing-description").text(nextTrack.author);
                this.updateInfo();
            }
        }
        this.send({ op: "skipTo", index: index });
    }

    backTo(index = 1) {
        // Patch 16: same idea as skipTo() above, for the previous track.
        if (index === 1 && this.canActImmediately()) {
            const prevTrack = this.queue[this.currentQueuePosition - 1];
            if (prevTrack) {
                this.isTrackLoading = true;
                this.loadingTrackId = prevTrack.trackId;
                this.updateImage("#now-playing-img", prevTrack.artworkUrl);
                $("#now-playing-title").text(prevTrack.title);
                $("#now-playing-description").text(prevTrack.author);
                this.updateInfo();
            }
        }
        this.send({ op: "backTo", index: index });
    }

    seekTo(tempPosition) {
        // Patch 25: updatePosition is @require_permission()'d (DJ only, no
        // vote fallback), so for a DJ it's safe to move the clock/bar to the
        // new spot now instead of waiting on the round trip.
        if (this.isDJ) {
            this.currentPosition = Math.trunc(tempPosition);
            this.updateInfo();
        }
        this.send({ op: "updatePosition", position: Math.trunc(tempPosition) });
    }

    shuffle() {
        if (this.queue.length - this.currentQueuePosition > 3) {
            this.send({ op: "shuffleTrack" });
        } else {
            this.tm.showToast(
                "info",
                localeTexts.errors.noEnoughTrackToShuffle
            );
        }
    }

    repeatMode() {
        // Patch 16: repeatTrack is @require_permission()'d (DJ/admin only,
        // no vote fallback) - a flat yes/no, so isDJ alone (no requester
        // bypass) is the right gate. Cycle order mirrors queue.py's
        // Repeat.next() exactly (cycles LoopType in declaration order:
        // OFF -> TRACK -> QUEUE -> OFF).
        if (this.isDJ) {
            const nextMode = { off: "track", track: "queue", queue: "off" };
            this.repeat = nextMode[this.repeat] || "off";
            this.updateInfo();
        }
        this.send({ op: "repeatTrack" });
    }

    send(payload) {
        // Guards against a literal accidental double-click/double-submit on
        // a search result or "add to queue" button sending the exact same
        // addTracks request twice in a row - with nothing visually changing
        // on click (no instant feedback, unlike the playback buttons fixed
        // elsewhere), a user waiting through Patch 11's now-much-longer
        // first-play caching wait can easily click again thinking the first
        // click didn't register, genuinely double-queueing the same track
        // server-side. Scoped tightly (same op+tracks+type within 1.5s) so
        // deliberately re-queueing the same song later is never blocked.
        if (payload.op === "addTracks" && this._isDuplicateAddTracksSend(payload)) {
            return;
        }
        this.socket.send(payload);
    }

    _isDuplicateAddTracksSend(payload) {
        const key = JSON.stringify([payload.tracks, payload.type]);
        const now = Date.now();
        const last = this._lastAddTracksSend;
        this._lastAddTracksSend = { key, time: now };
        return !!last && last.key === key && now - last.time < 1500;
    }

    isPlaying() {
        return this.currentTrack != undefined && this.isConnected;
    }

    updateSelectedBot(botId) {
        if (botId == null) {
            $(".bot-selection .selected .left").html(`<p></p>`);
            this.selectedBot = null;
        } else {
            var botId = botId.toString();
            var bot = this.bots.get(botId);
            if (bot != undefined) {
                $(".bot-selection .selected .left").html(
                    `<img src="${bot?.avatar}" alt=""><p>${bot?.name}</p>`
                );
                localStorage.setItem("selectedBot", botId);
                this.send({ op: "updateSelectedBot", botId: botId });
                this.selectedBot = bot;
            } else {
                this.tm.showToast("error", localeTexts.errors.selectBotError);
            }
        }
        this.updateSelectedBotView();
    }

    updateSelectedBotView() {
        let $view = $(".bot-selection .options");
        $view.empty();
        this.bots.forEach((value, key) => {
            if (value !== this.selectedBot) {
                $view.append(
                    `<div class="option" data-id="${value.id}">
                        <img src="${value.avatar}" alt="">
                        <p>${value.name}</p>
                    </div>`
                );
            }
        });
    }

    updateFilterView() {
        const filterView = $("#effect-panel");
        const isEnabled = this.filters.length != 0;

        let options = `
            <div class="option ${isEnabled ? "" : "active"}" data-id="none">
                <span class="material-symbols-outlined">block</span>
                <p>${localeTexts.effects.tags.none}</p>
            </div>
        `;

        this.availableFilters.forEach((element) => {
            const isActive = this.filters.some(
                (filter) => filter.tag === element.tag
            );
            const { icon = "help", text = "Unknown Effect" } =
                EFFECT_ICONS[element.tag] || {};

            options += `
                <div class="option ${isActive ? "active" : ""}" data-id="${element.tag
                }">
                    <span class="material-symbols-outlined">${icon}</span>
                    <p>${capitalize(text)}</p>
                </div>`;
        });

        filterView.html(`
            <div class="section" style="position: sticky;">
                <div class="info">
                    <h4>${localeTexts.effects.header}</h4>
                    <p>${localeTexts.effects.description}</p>
                </div>
            </div>

            <div class="options">${options}</div>
            <div class="control"></div>
        `);
    }

    updatePlaylistSelector() {
        let playlistSelector = $("#playlist-selector");
        playlistSelector.empty();
        Object.entries(this.playlists).forEach(([key, value]) => {
            playlistSelector.append(
                `<div class="menu-btn no-icon" id="menu-playlist-page-${key}" data-id="${key}">
                    <p>${value.name}</p>
                </div>`
            );
        });
    }

    updateCurrentQueuePos(pos) {
        const togglePlayNextSection = () => {
            const $playNextSection = $("#play-next-section");
            const shouldShow =
                this.queue.length - 1 > this.currentQueuePosition &&
                !$("#history-section").is(":visible");
            shouldShow ? $playNextSection.fadeIn() : $playNextSection.fadeOut();
        };

        if (pos !== undefined) {
            this.currentQueuePosition = pos - 1;
        }

        this.currentTrack = this.queue[this.currentQueuePosition];
        const $historySection = $("#history-section");
        const $nowPlayingSection = $("#now-playing-section");

        // Show or hide now playing section
        this.currentTrack
            ? $nowPlayingSection.fadeIn()
            : $nowPlayingSection.fadeOut();

        // Handle history section visibility
        if (this.currentQueuePosition < 1) {
            $historySection.fadeOut(200, () => {
                togglePlayNextSection();
            });
        } else {
            togglePlayNextSection();
        }

        // Update like button and position bar
        const isTrackInPlaylist = this.playlists?.["200"]?.["tracks"]?.includes(
            this.currentTrack?.trackId
        );
        this.likeBtn.toggleClass("filled", isTrackInPlaylist);
        this.positionBar.attr(
            "disabled",
            this.currentTrack?.isStream ? "disabled" : false
        );

        this.updateImage("#controller-img", this.currentTrack?.artworkUrl);
        this.updateImage("#now-playing-img", this.currentTrack?.artworkUrl);
        this.updateImage(
            "#now-playing-requester-img",
            this.currentTrack?.requester?.avatarUrl || '/static/img/notFound.png'
        );

        const colorThief = new ColorThief();
        const $img = $("#controller-img").clone();

        $(".main-grid-container").css("box-shadow", ``);
        if (this.currentTrack && this.currentTrack.source == "spotify") {
            $img[0].crossOrigin = "anonymous";

            $img.on("load", function () {
                const dominantColor = colorThief.getColor(this);
                let adjustedColor = dominantColor;

                if (
                    localStorage.getItem("theme") != "light" &&
                    !isDarkColor(dominantColor)
                ) {
                    adjustedColor = darkenColor(dominantColor, 0.3); // Darken by 30%
                }

                const rgbString = `rgba(${adjustedColor.join(",")}, 0.6)`;
                $(".main-grid-container").css({
                    "box-shadow": `inset 0 -90px 30px ${rgbString}`,
                });
            });
        }

        this.updateQueueList();
        return this.currentTrack;
    }

    updateInboxList() {
        const $inboxPanel = $("#inbox-panel .sections");
        $("#toggle-inbox-panel .btn").toggleClass(
            "active",
            this.inboxes.length > 0
        );
        $inboxPanel.empty();
        this.inboxes.forEach((mail) => {
            if (mail?.type !== "invite") return;

            let date = new Date(mail?.time * 1000);

            $inboxPanel.append(`
                <div class="message" data-id="${mail?.sender?.id}-${mail?.referId
                }">
                    <div>
                        <img src="${mail?.sender?.avatarUrl}"
                            alt="">
                    </div>
                    <div>
                        <p class="title">${mail?.title}</p>
                        <p class="time">${date.toDateString()} • ${date.toLocaleTimeString()}</p>
                        <p class="description">${mail?.description.replace(
                    "\n",
                    "<br>"
                )}</p>
                        <div class="actions">
                            <p class="action accept">${localeTexts.accept}</p>
                            <p class="action">${localeTexts.cancel}</p>
                        </div>
                    </div>
                </div>`);
        });

        if (!this.inboxes.length) {
            $inboxPanel.append(`
                <div class="no-message">
                    <img src="/static/img/noMessage.svg" alt="">
                    <p>${localeTexts.errors.noInboxMessage}</p>
                </div>`);
        }
    }

    updateQueueList() {
        removeContextMenu();
        // Patch 15: without a confirmed current track (hasActiveTrack),
        // currentQueuePosition is just its stale default (0) - nothing is
        // actually "current" to exclude yet, so the upcoming list should
        // start at index 0 (everything queued so far), not index 1.
        const upcomingStart = this.hasActiveTrack ? this.currentQueuePosition + 1 : this.currentQueuePosition;
        const historyEnd = this.hasActiveTrack ? this.currentQueuePosition : 0;
        $("#queue").html(
            this.queue
                .slice(upcomingStart)
                .map((track) => buildQueueTrackHtml(track, this.prefetchStatuses[track.trackId]))
        );

        $("#history-queue").html(
            this.queue
                .slice(0, historyEnd)
                .reverse()
                .map((track) => buildQueueTrackHtml(track))
                .join("")
        );
    }

    // Patch 22: patches one queue card's caching bar/checkmark in place
    // instead of rebuilding the whole #queue list (what updateQueueList()
    // does). prefetchStatus can fire many times a second while a track
    // downloads - re-rendering every card on every tick was destroying and
    // recreating the DOM elements Sortable.js tracks for drag-to-reorder,
    // so dragging a song up/down while anything else was caching looked
    // like the whole list kept refreshing out from under the drag.
    updateQueueTrackIndicator(trackId) {
        const $track = $(`#queue .track[data-id="${trackId}"]`);
        if (!$track.length) {
            // Not currently rendered (e.g. it's the track that's loading
            // right now - that's Now Playing's job via
            // updateNowPlayingPrefetchBar - or it's not queued at all).
            // The next structural re-render (trackLoading/trackUpdate/
            // addTrack) will pick up the right state once it matters again.
            return;
        }
        const { indicatorHtml, progressHtml } = buildPrefetchPiecesHtml(this.prefetchStatuses[trackId]);
        $track.find(".prefetch-progress-slot").html(progressHtml);
        $track.find(".prefetch-indicator-slot").html(indicatorHtml);
    }

    // Patch 13: the Now Playing panel's "this song is being cached" bar -
    // mirrors buildQueueTrackHtml()'s per-queue-item prefetch bar (action.js),
    // but driven by whichever track trackLoading last named (this.loadingTrackId)
    // rather than a queue position. Hidden/empty once that track finishes
    // loading (trackUpdate clears loadingTrackId) or its status leaves
    // "loading" (ready/unavailable) - see the prefetchStatus handler above.
    updateNowPlayingPrefetchBar() {
        const $bar = $("#now-playing-prefetch-bar");
        const state = this.loadingTrackId ? this.prefetchStatuses[this.loadingTrackId] : null;
        const hasProgress = state?.status === "loading" && typeof state.progress === "number";

        if (!hasProgress) {
            $bar.hide().empty();
            return;
        }

        const pct = Math.round(Math.max(0, Math.min(1, state.progress)) * 100);
        $bar.html(`
            <div class="prefetch-bar" title="Caching... ${pct}%"
                 style="height: 3px; border-radius: 2px; background: rgba(255,255,255,0.15); margin-top: 4px; overflow: hidden;">
              <div style="height: 100%; width: ${pct}%; background: currentColor; transition: width 0.3s linear;"></div>
            </div>
        `).show();
    }

    updateCacheStatsView() {
        const { entries, approxBytes } = this.cacheStats;
        const $line = $("#cache-stats-line");

        if (!entries) {
            return $line.fadeOut(150);
        }

        const size = approxBytes < 1024
            ? `${approxBytes} B`
            : `${(approxBytes / 1024).toFixed(1)} KB`;
        $line.text(`URL-metadata cache (not audio): ${entries} track${entries === 1 ? "" : "s"} · ${size}`);
        $line.fadeIn(150);
    }

    updateTime() {
        const trackLength = this.currentTrack?.length ?? 0;

        if (!this.currentTrack || this.currentPosition >= trackLength) {
            this.startTime.text("00:00");
            this.updateBar(this.positionBar, 0, 0);
            return this.timer.stop();
        }

        if (this.currentTrack?.isStream) {
            this.startTime.text("∞");
            this.updateBar(this.positionBar, 500, 100);
            return this.timer.stop();
        }

        this.currentPosition += 1000;
        this.startTime.text(msToReadableTime(this.currentPosition));

        const time = (this.currentPosition / trackLength) * 500;
        const progress = (time / this.positionBar.attr("max")) * 100;

        this.updateBar(this.positionBar, time, progress);
    }

    // Patch 27: keeps the slider's fill and the number next to it in step
    // (fill is relative to the slider's max, currently 100). Skipped while this user is
    // mid-drag so someone else's change doesn't yank the slider out from
    // under them - their own release sends the final value anyway.
    updateVolumeView(volume) {
        if (typeof volume !== "number") return;
        if (this.isAdjustingVolume) return;
        const max = Number(this.volumeBar.attr("max")) || 100;
        this.updateBar(this.volumeBar, volume, (volume / max) * 100);
        updateVolumeIndicator(volume);
    }

    updateBar(bar, time, progress) {
        bar.val(time);
        bar.css(
            "background",
            `linear-gradient(to right, var(--primary) ${progress}%, var(--text-muted) ${progress}%)`
        );
    }

    updateInfo() {
        const currentTrack = this.currentTrack;
        const elementsToUpdate = {
            "#controller-title": currentTrack?.title || "",
            "#controller-description": currentTrack?.author || "",
            "#now-playing-title": currentTrack?.title || "",
            "#now-playing-description": currentTrack?.author || "",
            "#end-time": currentTrack
                ? currentTrack?.isStream
                    ? "∞"
                    : msToReadableTime(currentTrack.length)
                : "00:00",
            "#play-pause-btn":
                this.isPaused || !currentTrack ? "play_circle" : "pause_circle",
            "#repeat-btn": this.repeat == "track" ? "repeat_one" : "repeat",
        };

        Object.keys(elementsToUpdate).forEach((selector) => {
            const $element = $(selector);
            const newValue = elementsToUpdate[selector];
            if ($element.text() !== newValue) {
                $element.text(newValue);
            }
        });

        $("#autoplay-btn").toggleClass("active", this.autoplay)
        $("#controller-loader, #now-playing-loader").toggle(this.isTrackLoading)

        if (this.isPaused || !currentTrack) {
            this.timer.stop();
            this.updateTime();
        } else {
            this.timer.start();
        }

        $("#control-info").css("opacity", currentTrack ? "1" : "0");
        this.repeat == "off"
            ? $("#repeat-btn").removeClass("active")
            : $("#repeat-btn").addClass("active");
    }

    updateImage(selector, artworkUrl) {
        if (artworkUrl) {
            $(selector).fadeIn(200, function () {
                $(this).attr("src", artworkUrl);
            });
        } else {
            $(selector).fadeOut(200, function () {
                $(this).removeAttr("src");
            });
        }
    }

    updateChannelMemberView() {
        const channelMemberView = $("#channel-members");
        const usersList = Object.values(this.users).filter(
            (user) => user.userId !== this.userId
        );

        channelMemberView.empty();

        usersList.slice(0, 4).forEach((user) => {
            channelMemberView.append(`
                <div class="avatar">
                    <p class="name">${user.name}</p>
                    <img src="${user.avatarUrl}" alt="${user.name}">
                </div>
            `);
        });

        if (usersList.length > 4) {
            const extraCount = usersList.length - 4;
            channelMemberView.append(`
                <div class="avatar extra">
                    <p class="name">+${extraCount} more ...</p>
                    <p>+${extraCount}</p>
                </div>
            `);
        }
    }
}
