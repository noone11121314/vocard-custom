from __future__ import annotations

import logging, time, re

from typing import Any, List, Dict, Union, Optional, TYPE_CHECKING

from discord import User, Member, Guild, NotFound, HTTPException
from discord.ext import commands
from voicelink import Player, Track, Playlist, NodePool, LoopType, Filters, Config, MongoDBHandler, LangHandler, LYRICS_PLATFORMS
from voicelink.utils import TempCtx

logger = logging.getLogger("vocard")

if TYPE_CHECKING:
    from .client import IPCClient
    
RATELIMIT_COUNTER: Dict[int, Dict[str, float]] = {}
SCOPES = {
    "prefix": str,
    "lang": str,
    "queue_type": str,
    "dj": int,
    "controller": bool,
    "controller_msg": bool,
    "24/7": bool,
    "disabled_vote": bool,
    "duplicate_track": bool,
    "silent_msg": bool,
    "default_controller": dict,
    "stage_announce_template": str
}

class SystemMethod:
    def __init__(self, function: callable, *, credit: int = 1):
        self.function: callable = function
        self.params: List[str] = ["bot", "data"]
        self.credit: int = credit

class PlayerMethod(SystemMethod):
    def __init__(self, function, *, credit: int = 1, auto_connect: bool = False):
        super().__init__(function, credit=credit)
        self.params: List[str] = ["player", "member", "data"]
        self.auto_connect: bool = auto_connect

async def _is_privileged_fresh(player: Player, member: Member, check_user_join: bool = True) -> bool:
    """Patch 32: player.is_privileged(), re-checked once against the member's
    live roles from Discord when a DJ role is configured but the bot's cached
    Member doesn't have it. The cache can lag behind role changes, and a real
    DJ (the role checked out on Discord's side) was refused every queue edit
    and shown as non-DJ on the dashboard. One REST call, only on a refusal;
    the fresh member also replaces the stale cached one."""
    if player.is_privileged(member, check_user_join=check_user_join):
        return True
    dj_role = player.settings.get("dj")
    if not dj_role or any(role.id == dj_role for role in member.roles):
        return False
    try:
        fresh = await member.guild.fetch_member(member.id)
    except (NotFound, HTTPException):
        return False
    if not any(role.id == dj_role for role in fresh.roles):
        return False
    logger.info(f"DJ check: cached roles for {member} ({member.id}) were stale - Discord says they have the DJ role")
    if hasattr(member.guild, "_add_member"):
        member.guild._add_member(fresh)
    return player.is_privileged(fresh, check_user_join=check_user_join)

def require_permission(only_admin: bool = False):
    def decorator(func) -> callable:
        async def wrapper(player: Player, member: Member, dict: Dict) -> Optional[Dict]:
            if only_admin and not member.guild_permissions.manage_guild:
                return error_msg("Only the admins may use this function!", user_id=member.id)
            if not await _is_privileged_fresh(player, member):
                return error_msg("Only the DJ or admins may use this function!", user_id=member.id)
            return await func(player, member, dict)
        return wrapper
    return decorator

def error_msg(msg: str, *, user_id: int = None, guild_id: int = None, level: str = "info") -> Dict:
    payload = {"op": "errorMsg", "level": level, "msg": msg}
    if user_id:
        payload["userId"] = str(user_id)
    if guild_id:
        payload["guildId"] = str(guild_id)

    return payload

async def connect_channel(member: Member, bot: commands.Bot) -> Player:
    """Diagnostic note (2026-10-18): this used to have a bare `except: return`
    here, silently swallowing *any* failure to join the voice channel
    (missing Connect/Speak permission, wrong voice region, a Lavalink
    hiccup, anything) with zero logging - from the dashboard's side, and
    from every log, this looked identical to "nothing happened at all" for
    addTracks/searchAndPlay (both auto_connect=True), making a real failure
    here indistinguishable from the member simply not being in a voice
    channel. Now logs the real exception at debug level before still
    returning None the same way, so a future case like this is diagnosable
    without having to read this function's source to even suspect it.
    """
    if not member.voice:
        return

    channel = member.voice.channel
    try:
        settings = await MongoDBHandler.get_settings(channel.guild.id)
        player: Player = await channel.connect(cls=Player(bot, channel, TempCtx(member, channel), settings))
        await player.send_ws({"op": "createPlayer", "memberIds": [str(member.id) for member in channel.members]})
        return player
    except Exception as e:
        logger.debug(f"connect_channel failed to join {channel.name} ({channel.guild.name}) for {member}: {e}", exc_info=e)
        return

async def initBot(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))
    user = bot.get_user(user_id)
    if not user:
        user = await bot.fetch_user(user_id)

    if user:
        return {
            "op": "initBot",
            "userId": str(user_id),
            "botName": bot.user.display_name,
            "botAvatar": bot.user.display_avatar.url,
            "botId": str(bot.user.id)
        }
    
async def initUser(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))
    data = await MongoDBHandler.get_user(user_id)

    for mail in data.get("inbox"):
        sender = bot.get_user(mail.get("sender"))
        if not sender:
            sender = await bot.fetch_user(mail.get("sender"))

        if not sender:
            data.get("inbox").remove(mail)

        mail["sender"] = {"avatarUrl": sender.display_avatar.url, "name": sender.display_name, "id": str(sender.id)}

    return {
        "op": "initUser",
        "userId": str(user_id),
        "data": data
    }
    
async def initPlayer(player: Player, member: Member, data: Dict) -> Dict:
    player._ipc_connection = True
    available_filters = []
    for name, filter_cls in Filters.get_available_filters().items():
        filter = filter_cls()
        available_filters.append({"tag": name, "scope": filter.scope, "payload": filter.payload})

    # One-shot sync of the disk-cache prefetch indicators (ticks/progress
    # bars) and the resolved-URL cache line, so a fresh page load/reconnect
    # shows the current state immediately instead of staying blank until
    # the next prefetchStatus/cacheStats push - those are only sent when
    # something *changes*, which a brand-new websocket connection missed
    # (see docs/local-patches.md's Patch 7 follow-up).
    from voicelink import stream_cache
    disk_entries = {e["videoId"]: e for e in await stream_cache.list_entries()}
    prefetch_statuses = {}
    for track in player.queue._queue:
        if track.source != "youtube" or not track.identifier:
            continue
        entry = disk_entries.get(track.identifier)
        if entry is None or entry["status"] == "failed":
            continue
        if entry["status"] == "ready":
            prefetch_statuses[track.track_id] = {"status": "ready"}
        else:
            prefetch_statuses[track.track_id] = {"status": "loading", "progress": entry.get("progress") or 0}

    return {
        "op": "initPlayer",
        "guildId": str(player.guild.id),
        "userId": str(data.get("userId")),
        "users": [{
            "userId": str(member.id),
            "avatarUrl": member.display_avatar.url,
            "name": member.name
        } for member in player.channel.members ],
        "tracks": [ {"trackId": track.track_id, "requesterId": str(track.requester.id)} for track in player.queue._queue ],
        "repeatMode": player.queue.repeat.lower(),
        "channelName": player.channel.name,
        # has_dequeued_track (not just is_playing): while a track is still
        # being pre-cached (Patch 11) Queue.get() has already advanced
        # _position past it but _current isn't set yet - treating that as
        # "nothing playing" pointed the dashboard one track too far.
        "currentQueuePosition": player.queue._position + (0 if player.has_dequeued_track else 1),
        "hasCurrentTrack": player.has_dequeued_track,
        "loadingTrackId": player.loading_track.track_id if player.loading_track else None,
        "currentPosition": 0 or player.position if player.is_playing else 0,
        "isPlaying": player.is_playing,
        "isPaused": player.is_paused,
        "isDj": await _is_privileged_fresh(player, member, check_user_join=False),
        "autoplay": player.settings.get("autoplay", False),
        "volume": player.volume,
        "filters": [{"tag": filter.tag, "scope": filter.scope, "payload": filter.payload} for filter in player.filters.get_filters()],
        "availableFilters": available_filters,
        "prefetchStatuses": prefetch_statuses,
        "cacheStats": player._node.get_direct_resolve_cache_stats() if player._node else {"entries": 0, "approxBytes": 0}
    }

async def closeConnection(bot: commands.Bot, data: Dict) -> None:
    guild_id = int(data.get("guildId"))
    guild = bot.get_guild(guild_id)
    player: Player = guild.voice_client
    if player:
        player._ipc_connection = False

async def getRecommendation(bot: commands.Bot, data: Dict) -> None: 
    node = NodePool.get_node()
    if not node:
        return
    
    track_data = Track.decode(track_id := data.get("trackId"))
    track = Track(track_id=track_id, info=track_data, requester=bot.user)
    tracks: List[Track] = await track.get_recommendations(node)

    return {
        "op": "getRecommendation",
        "userId": str(data.get("userId")),
        "callback": data.get("callback"),
        "tracks": [track.track_id for track in tracks] if tracks else []
    }

async def skipTo(player: Player, member: Member, data: Dict) -> None:
    if not await _is_privileged_fresh(player, member):
        if player.current and member == player.current.requester:
            pass

        elif member in player.skip_votes:
            return error_msg(player.get_msg('voting.voted'), user_id=member.id)
        
        else:
            player.skip_votes.add(member)
            if len(player.skip_votes) < (required := player.required()):
                return error_msg(player.get_msg('player.controls.skip.vote').format(member, len(player.skip_votes), required), guild_id=player.guild.id)

    index = data.get("index", 1)
    if index > 1:
        player.queue.skipto(index)

    if player.queue._repeat.mode == LoopType.TRACK:
        await player.set_repeat(LoopType.OFF)
    await player.stop()

async def backTo(player: Player, member: Member, data: Dict) -> None:
    if not await _is_privileged_fresh(player, member):
        if player.current and member == player.current.requester:
            pass

        elif member in player.skip_votes:
            return error_msg(player.get_msg('voting.voted'), user_id=member.id)
        
        else:
            player.skip_votes.add(member)
            if len(player.skip_votes) < (required := player.required()):
                return error_msg(player.get_msg('player.controls.back.vote').format(member, len(player.skip_votes), required), guild_id=player.guild.id)
    
    index = data.get("index", 1)
    if not player.is_playing:
        player.queue.backto(index)
        await player.do_next()
    else:
        player.queue.backto(index + 1)
        await player.stop()

async def moveTrack(player: Player, member: Member, data: Dict) -> None:
    index = data.get("index")
    new_index = data.get("newIndex")
    if index == new_index:
        return

    await player.move_track(index, new_index, member)

async def addTracks(player: Player, member: Member, data: Dict) -> None:
    _type = data.get("type", "addToQueue")
    tracks = [Track(
        track_id=track_id, 
        info=Track.decode(track_id),
        requester=member
    ) for track_id in data.get("tracks", [])]

    if _type == "addToQueue":
        await player.add_track(tracks)

    elif _type == "forcePlay":
        await player.add_track(tracks, at_front=True)
        if player.is_playing:
            return await player.stop()
    
    elif _type == "addNext":
        await player.add_track(tracks, at_front=True)

    if not player.is_playing:
        await player.do_next()

async def getTracks(bot: commands.Bot, data: Dict) -> Dict:
    query = data.get("query", None)

    if query:
        payload = {"op": "getTracks", "userId": data.get("userId"), "callback": data.get("callback")}
        tracks = await NodePool.get_node().get_tracks(query=query, requester=None)
        if not tracks:
            return payload

        payload["tracks"] = [ track.track_id for track in (tracks.tracks if isinstance(tracks, Playlist) else tracks ) ]
        return payload

async def searchAndPlay(player: Player, member: Member, data: Dict) -> None:
    payload = await getTracks(player.bot, data)
    await addTracks(player, member, payload)

async def shuffleTrack(player: Player, member: Member, data: Dict) -> None:
    if not await _is_privileged_fresh(player, member):
        if member in player.shuffle_votes:
            return error_msg(player.get_msg('voting.voted'), user_id=member.id) 

        player.shuffle_votes.add(member)
        if len(player.shuffle_votes) < (required := player.required()):
            return error_msg(player.get_msg('player.controls.shuffle.vote').format(member, len(player.skip_votes), required), guild_id=player.guild.id)
    
    await player.shuffle(data.get("type", "queue"), member)

@require_permission()
async def repeatTrack(player: Player, member: Member, data: Dict) -> None:
    await player.set_repeat(requester=member)

@require_permission()
async def removeTrack(player: Player, member: Member, data: Dict) -> None:
    index, index2 = data.get("index"), data.get("index2")
    await player.remove_track(index, index2, requester=member, expected_track_id=data.get("trackId"))

@require_permission()
async def clearQueue(player: Player, member: Member, data: Dict) -> None:
    queue_type = data.get("queueType", "").lower()
    await player.clear_queue(queue_type, member)

async def updateVolume(player: Player, member: Member, data: Dict) -> None:
    if "mute" in data:
        # Patch 42: the dashboard's speaker icon - same behaviour as the
        # Discord controller's mute button. Muting doesn't save 0 as the
        # guild's volume, so unmuting (from either place) restores it.
        if data["mute"]:
            await player.set_volume(0, requester=member, persist=False)
            return
        saved = int(player.settings.get("volume", 100) or 0)
        if saved <= 0:  # the saved level itself is 0 (slider dragged to 0)
            try:
                saved = max(1, min(100, int(data.get("volume") or 50)))
            except (TypeError, ValueError):
                saved = 50
        await player.set_volume(saved, requester=member)
        return
    try:
        volume = max(0, min(100, int(data.get("volume", 100))))  # Patch 28: 0-100, same as /volume
    except (TypeError, ValueError):
        return
    await player.set_volume(volume=volume, requester=member)  # saves it too (Patch 27)

async def updatePause(player: Player, member: Member, data: Dict) -> None:
    pause = data.get("pause", True)
    if not await _is_privileged_fresh(player, member):
        if pause:
            if member in player.pause_votes:
                return error_msg(player.get_msg('voting.voted'), user_id=member.id)

            player.pause_votes.add(member)
            if len(player.pause_votes) < (required := player.required()):
                return error_msg(player.get_msg('player.controls.pause.vote').format(member, len(player.pause_votes), required), guild_id=player.guild.id)

        else:
            if member in player.resume_votes:
                return error_msg(player.get_msg('voting.voted'), user_id=member.id)
            
            player.resume_votes.add(member)
            if len(player.resume_votes) < (required := player.required()):
                return error_msg(player.get_msg('player.controls.resume.vote').format(member, len(player.resume_votes), required), guild_id=player.guild.id)

    await player.set_pause(pause, member)

@require_permission()
async def updatePosition(player: Player, member: Member, data: Dict) -> None:
    position = data.get("position");
    await player.seek(position, member);

async def toggleAutoplay(player: Player, member: Member, data: Dict) -> Dict:
    if not await _is_privileged_fresh(player, member):
        return error_msg(player.get_msg('permissions.missingAutoPlay'))

    check = data.get("status", False)
    player.settings['autoplay'] = check

    if not player.is_playing:
        await player.do_next()

    return {
        "op": "toggleAutoplay",
        "status": check,
        "guildId": str(player.guild.id),
        "requesterId": str(member.id)
    }

@require_permission()
async def updateFilter(player: Player, member: Member, data: Dict) -> None:
    updateType = data.get("type", "add")
    filter_tag = data.get("tag")

    if updateType == "add":
        available_filters = Filters.get_available_filters()
        filter_cls = available_filters.get(filter_tag)
        if not filter_cls:
            return
        
        payload = {}
        if "payload" in data:
            payload = data.get("payload").get(list(data.get("payload").keys()[0]), {})
        if player.filters.has_filter(filter_tag=filter_tag):
            player.filters.remove_filter(filter_tag=filter_tag)
        await player.add_filter(filter=filter_cls(**payload), requester=member)

    elif updateType == "remove":
        await player.remove_filter(filter_tag=filter_tag, requester=member)

    else:
        await player.reset_filter(requester=member)

async def _loadPlaylist(playlist: Dict) -> Optional[List[Track]]:
    if playlist.get("type") == "link":
        tracks: List[Track]= await NodePool.get_node().get_tracks(playlist.get("uri"), requester=None)
        if tracks:
            return [track.track_id for track in (tracks.tracks if isinstance(tracks, Playlist) else tracks)]
    else:
        return playlist.get("tracks", [])

def _assign_playlist_id(existed: list) -> str:
    for i in range(200, 210):
        if str(i) not in existed:
            return str(i)
        
async def _getPlaylist(user_id: int, playlist_id: str) -> Dict:
    playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
    playlist = playlists.get(playlist_id)
    if not playlist:
        return
    
    if playlist["type"] == "share":
        target_user = await MongoDBHandler.get_user(playlist["user"], d_type="playlist")
        target_playlist = target_user.get(playlist["referId"])
        if target_playlist and user_id in target_playlist.get("perms", {}).get("read", []):
            playlist["tracks"] = await _loadPlaylist(target_playlist)
    else:
        playlist["tracks"] = await _loadPlaylist(playlist)

    return playlist

async def getPlaylist(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))
    playlist_id = str(data.get("playlistId"))

    payload = {"op": "loadPlaylist", "playlistId": playlist_id, "userId": str(user_id)}
    playlist = await _getPlaylist(user_id, playlist_id)
    payload["tracks"] = playlist["tracks"] if playlist else []
    
    return payload
    
async def updatePlaylist(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))
    playlist_id = str(data.get("playlistId"))
    _type = data.get("type")
    
    if not playlist_id and not _type == "createPlaylist":
        return error_msg("Unable to process this request without a playlist ID.", user_id=user_id, level="error")
    
    max_p, max_t, _ = Config().get_playlist_config()
    if _type == "createPlaylist":
        name, playlist_url = data.get("playlistName"), data.get("playlistUrl")
        if not name:
            return {
                "op": "updatePlaylist",
                "status": "error",
                "msg": f"You must enter name for this field!",
                "field": "playlistName",
                "userId": str(user_id)
            }
        
        playlist = await MongoDBHandler.get_user(user_id, d_type="playlist")
        if len(list(playlist.keys())) >= max_p:
            return {
                "op": "updatePlaylist",
                "status": "error",
                "msg": f"You cannot create more than '{max_p}' playlists!",
                "field": "playlistName",
                "userId": str(user_id)
            }

        for playlist_data in playlist.values():
            if playlist_data['name'].lower() == name.lower():
                return {
                    "op": "updatePlaylist",
                    "status": "error",
                    "msg": f"Playlist '{name}' already exists.",
                    "field": "playlistName",
                    "userId": str(user_id)
                }
        
        if playlist_url:
            tracks = await NodePool.get_node().get_tracks(playlist_url, requester=None)
            if not isinstance(tracks, Playlist):
                return {
                    "op": "updatePlaylist",
                    "status": "error",
                    "msg": f"Please enter a valid link or public playlist link.",
                    "field": "playlistUrl",
                    "userId": str(user_id)
                }

        assigned_playlist_id = _assign_playlist_id(list(playlist.keys()))
        data = {'uri': playlist_url, 'perms': {'read': []}, 'name': name, 'type': 'link'} if playlist_url else {'tracks': [], 'perms': {'read': [], 'write': [], 'remove': []}, 'name': name, 'type': 'playlist'}
        await MongoDBHandler.update_user(user_id, {"$set": {f"playlist.{assigned_playlist_id}": data}})
        return {
            "op": "updatePlaylist",
            "status": "created",
            "playlistId": assigned_playlist_id,
            "msg": f"You have created '{name}' playlist.",
            "userId": str(user_id),
            "data": data
        }
        
    elif _type == "removePlaylist":
        playlist = await _getPlaylist(user_id, playlist_id)
        if playlist:
            if playlist['type'] == 'share':
                await MongoDBHandler.update_user(playlist['user'], {"$pull": {f"playlist.{playlist['referId']}.perms.read": user_id}})

            await MongoDBHandler.update_user(user_id, {"$unset": {f"playlist.{playlist_id}": 1}})

        return {
            "op": "updatePlaylist",
            "status": "deleted",
            "playlistId": playlist_id,
            "msg": f"You have removed playlist '{playlist['name']}'",
            "userId": str(user_id)
        }
    
    elif _type == "renamePlaylist":
        name = data.get("name")
        if not name:
            return {
                "op": "updatePlaylist",
                "status": "error",
                "msg": f"You must enter name for this field!",
                "field": "playlistName",
                "userId": str(user_id)
            }
        
        playlist = await MongoDBHandler.get_user(user_id, d_type="playlist")
        for data in playlist.values():
            if data['name'].lower() == name.lower():
                return {
                    "op": "updatePlaylist",
                    "status": "error",
                    "msg": f"Playlist '{data['name']}' already exists.",
                    "field": "playlistName",
                    "userId": str(user_id)
                }

        await MongoDBHandler.update_user(user_id, {"$set": {f'playlist.{playlist_id}.name': name}})
        return {
            "op": "updatePlaylist",
            "status": "renamed",
            "name": name,
            "playlistId": playlist_id,
            "msg": f"You have renamed the playlist to '{name}'.",
            "field": "playlistName",
            "userId": str(user_id)
        }
    
    elif _type == "addTrack":
        track_id = data.get("trackId")
        if not track_id:
            return error_msg("No track ID could be located.", user_id=user_id, level='error')
        
        playlist = await _getPlaylist(user_id, playlist_id)
        if playlist['type'] in ['share', 'link']:
            return error_msg("You cannot add songs to a linked playlist through Vocard.", user_id=user_id, level='error')
        
        if len(playlist['tracks']) >= max_t:
            return error_msg(f"You have reached the limit! You can only add {max_t} songs to your playlist.", user_id=user_id)

        decoded_track = Track(track_id=track_id, info=Track.decode(track_id), requester=None)
        if decoded_track.is_stream:
            return error_msg("You are not allowed to add streaming videos to your playlist.", user_id=user_id)
        from voicelink import cantus
        if cantus.is_foreign(decoded_track, int(user_id)):  # Patch 46
            return error_msg(cantus.FOREIGN_NOTE, user_id=user_id)
        
        await MongoDBHandler.update_user(user_id, {"$push": {f'playlist.{playlist_id}.tracks': track_id}})
        return {
            "op": "updatePlaylist",
            "status": "addTrack",
            "playlistId": playlist_id,
            "trackId": track_id,
            "msg": f"Added {decoded_track.title} into '{playlist['name']}' playlist.",
            "userId": str(user_id)
        }
        
    elif _type == "removeTrack":
        track_id, track_position = data.get("trackId"), data.get("trackPosition", 0)
        if not track_id:
            return error_msg("No track ID could be located.", user_id=user_id, level='error')
        
        playlist = await _getPlaylist(user_id, playlist_id)
        if not playlist:
            return error_msg("Playlist not found!", user_id=user_id, level='error')
        
        if playlist['type'] in ['share', 'link']:
            return error_msg("You cannot remove songs from a linked playlist through Vocard.", user_id=user_id, level='error')
        
        if not 0 <= track_position < len(playlist['tracks']):
            return error_msg("Cannot find the position from your playlist.", user_id=user_id, level="error")

        if playlist['tracks'][track_position] != track_id:
            return error_msg("Something wrong while removing the track from your playlist.", user_id=user_id, level='error')
        
        await MongoDBHandler.update_user(user_id, {"$pull": {f'playlist.{playlist_id}.tracks': playlist['tracks'][track_position]}})
        
        decoded_track = Track.decode(playlist['tracks'][track_position])
        return {
            "op": "updatePlaylist",
            "status": "removeTrack",
            "playlistId": playlist_id,
            "trackPosition": track_position,
            "trackId": track_id,
            "msg": f"Removed '{decoded_track['title']}' from '{playlist['name']}' playlist.",
            "userId": str(user_id)
        }

    elif _type == "updateInbox":
        user = await MongoDBHandler.get_user(user_id)
        is_accept = data.get("accept", False)

        if is_accept and len(list(user.get("playlist").keys())) >= max_p:
            return error_msg(f"You cannot create more than '{max_p}' playlists!", user_id=user_id, level = "error")

        info = data.get("referId", "").split("-")
        sender_id, refer_id = info[0], info[1]
        inbox = user.get("inbox")

        payload = {"op": "updatePlaylist", "status": "updateInbox", "userId": str(user_id), "accept": is_accept, "senderId": sender_id, "referId": refer_id}
        for index, mail in enumerate(inbox.copy()):
            if not (str(mail.get("sender")) == sender_id and mail.get("referId") == refer_id):
                continue
            
            del inbox[index]
            if is_accept:
                share_playlists = await MongoDBHandler.get_user(mail["sender"], d_type="playlist")
                if refer_id not in share_playlists:
                    return error_msg("The shared playlist couldn’t be found. It’s possible that the user has already deleted it.", user_id=user_id)
                
                assigned_playlist_id = _assign_playlist_id(list(user.get("playlist", []).keys()))
                playlist_name = f"Share{time.strftime('%M%S', time.gmtime(int(mail['time'])))}"
                share_playlist = share_playlists.get(refer_id)
                share_playlist.update({
                    "name": playlist_name,
                    "type": "share"
                })
                await MongoDBHandler.update_user(mail['sender'], {"$push": {f"playlist.{mail['referId']}.perms.read": user_id}})
                await MongoDBHandler.update_user(user_id, {"$set": {
                    f'playlist.{assigned_playlist_id}': {
                        'user': mail['sender'], 'referId': mail['referId'],
                        'name': playlist_name,
                        'type': 'share'
                    },
                    "inbox": inbox
                }})

                payload.update({
                    "playlistId": assigned_playlist_id,
                    "msg": f"You have created '{playlist_name}' playlist.",
                    "data": share_playlist,
                })

            await MongoDBHandler.update_user(user_id, {"$set": {"inbox": inbox}})
            return payload

async def getMutualGuilds(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))

    payload = {"op": "getMutualGuilds", "mutualGuilds": {}, "inviteGuilds": {}, "userId": str(user_id)}
    for guild_id, guild_info in data.get("guilds", {}).items():
        if guild := bot.get_guild(int(guild_id)):
            payload["mutualGuilds"][guild_id] = {
                **guild_info,
                "memberCount": guild.member_count
            }
        else:
            payload["inviteGuilds"][guild_id] = {**guild_info}

    return payload

async def getSettings(bot: commands.Bot, data: Dict) -> Dict:
    user_id = int(data.get("userId"))
    guild_id  = int(data.get("guildId"))

    guild = bot.get_guild(guild_id)
    if not guild:
        return error_msg("Vocard don't have access to requested guild.", user_id=user_id, level="error")

    member = guild.get_member(user_id)
    if not member:
        return error_msg("You are not in the requested guild.", user_id=user_id, level="error")
    
    if not member.guild_permissions.manage_guild:
        return error_msg("You don't have permission to access the settings.", user_id=user_id, level='error')
    
    settings = await MongoDBHandler.get_settings(guild_id)
    if "dj" in settings:
        role = guild.get_role(settings["dj"])
        if role:
            settings["dj"] = role.name

    return {
        "op": "getSettings",
        "settings": settings,
        "options": {
            "languages": list(LangHandler.get_all_languages()),
            "queueModes": ["Queue", "FairQueue"],
            "roles": [role.name for role in guild.roles]
        },
        "guild": {
            "avatar": guild.icon.url if guild.icon else None,
            "name": guild.name,
            "id": str(guild_id)
        },
        "userId": str(user_id)
    }

def _resolve_guild_for_user(bot: commands.Bot, user_id: int, data: Dict) -> Optional[Guild]:
    """Patch 9: best-effort "which guild is this dashboard request about"
    resolver for home/explore-page calls that aren't tied to an active
    voice-channel player (so they can't rely on `process_methods`'
    PlayerMethod guild/member resolution, which also requires the user to
    currently be sitting in the bot's voice channel - wrong requirement
    for a page someone can browse without joining a call).

    Tries `data["guildId"]` first (the dashboard sends this once it knows
    one, e.g. from a prior `initPlayer`), same `int(...)` + try/except
    pattern as `_require_cache_manager_permission` below so a missing/
    stale guildId never crashes the request. Falls back to the first of
    the user's mutual guilds where they're still a member. Only checks
    membership, not DJ/admin privilege - play history is read-only and
    not sensitive, so any shared-server member may view it.
    """
    try:
        preferred_guild_id = int(data.get("guildId"))
    except (TypeError, ValueError):
        preferred_guild_id = None

    candidate_guilds = []
    seen_guild_ids = set()
    if preferred_guild_id is not None and (guild := bot.get_guild(preferred_guild_id)):
        candidate_guilds.append(guild)
        seen_guild_ids.add(guild.id)

    if user := bot.get_user(user_id):
        candidate_guilds.extend(g for g in user.mutual_guilds if g.id not in seen_guild_ids)

    for guild in candidate_guilds:
        if guild.get_member(user_id):
            return guild

    return None

def _top_tracks_from_counts(counts: Dict[str, Any], limit: int = 20) -> List[str]:
    """Patch 9: ranks a `{hashedKey: {"count", "trackId", "title"}}` map
    (as written by `Player._record_guild_play_history`) by play count,
    returning just the `trackId` strings the frontend needs to re-decode
    and render - highest play count first.
    """
    ranked = sorted(
        (entry for entry in (counts or {}).values() if isinstance(entry, dict) and entry.get("trackId")),
        key=lambda entry: entry.get("count", 0),
        reverse=True
    )
    return [entry["trackId"] for entry in ranked[:limit]]

async def getGuildHistory(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 9: powers the dashboard Home page's "Recently played" section.
    Returns this guild's two recency lists (capped at 60 each by
    `Player._record_guild_play_history`) split by source: tracks searched
    and played normally ("bot") vs. tracks played from a Discord message
    attachment ("upload"). Replaces the old per-user, 25-track personal
    history, which only reflected what one person requested rather than
    everything that's actually been played in the server.
    """
    user_id = int(data.get("userId"))
    guild = _resolve_guild_for_user(bot, user_id, data)
    if not guild:
        return {"op": "getGuildHistory", "userId": str(user_id), "botHistory": [], "uploadHistory": []}

    settings = await MongoDBHandler.get_settings(guild.id)
    return {
        "op": "getGuildHistory",
        "userId": str(user_id),
        "guildId": str(guild.id),
        "guildName": guild.name,  # Patch 44: labels the server lists on Home
        "botHistory": settings.get("botHistory", []),
        "uploadHistory": settings.get("uploadHistory", []),
    }

async def getGuildAddedHistory(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 17: powers the dashboard Home page's "Recently added" section -
    every track the moment it's queued (Player._record_guild_added_history),
    regardless of whether it ever actually played, capped at 100. A
    separate, broader list than getGuildHistory's "Recently played" above,
    which only logs a track once it starts playing.
    """
    user_id = int(data.get("userId"))
    guild = _resolve_guild_for_user(bot, user_id, data)
    if not guild:
        return {"op": "getGuildAddedHistory", "userId": str(user_id), "addedHistory": []}

    settings = await MongoDBHandler.get_settings(guild.id)
    return {
        "op": "getGuildAddedHistory",
        "userId": str(user_id),
        "guildId": str(guild.id),
        "guildName": guild.name,
        "addedHistory": settings.get("addedHistory", []),
    }

async def getUserHistory(bot: commands.Bot, data: Dict) -> Dict:
    """A separate, personal "Recently played by you" log - as opposed to
    getGuildHistory above, which is guild-wide and shows every member's
    plays. Player.do_next() has always written every non-bot requester's
    play to their own Mongo user document ($push "history", capped at 25)
    for exactly this purpose, it just was never surfaced anywhere in the
    dashboard until now.
    """
    user_id = int(data.get("userId"))
    history = await MongoDBHandler.get_user(user_id, d_type="history")
    added = await MongoDBHandler.get_user(user_id, d_type="addedHistory")  # Patch 44
    return {
        "op": "getUserHistory",
        "userId": str(user_id),
        "history": history or [],
        "addedHistory": added or [],
    }

_CANTUS_MESSAGES = {
    "token_revoked": "The token was refused (revoked or mistyped).",
    "bad_token": "That isn't a Cantus connect code or token.",
    "bad_code": "That isn't a Cantus connect code or token.",
    "no_default_server": "Paste the whole connect code (there's no default Cantus server set).",
    "not_linked": "You aren't connected to Cantus yet.",
    "bad_url": "That server address isn't valid.",
    "private_address": "That server address isn't allowed (it must be a public address).",
    "unresolvable": "That server address couldn't be found.",
}


def _cantus_message(e, server: str = None) -> str:
    if e.code in _CANTUS_MESSAGES:
        return _CANTUS_MESSAGES[e.code]
    if e.status == 401:
        return _CANTUS_MESSAGES["token_revoked"]
    if server and (e.status == 0 or e.status in (404, 405) or e.status >= 500):
        return f"Cantus at {server} didn't answer."
    if e.status in (404, 405) or e.status >= 500:
        return "That server didn't answer like Cantus does - check the address."
    return "Couldn't reach that server - check the address and try again."


async def _is_bot_admin(bot: commands.Bot, user_id: int, data: Dict = None) -> bool:
    """Patch 46: who may set the default Cantus server - the bot's owner on
    Discord (the application's owner or team), a bot_access_user, or a DJ /
    server admin in any server the bot shares with them (the cache
    manager's check). It's bot-wide, so any of them sets it for every server."""
    if user_id in Config().bot_access_user:
        return True
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user and await bot.is_owner(user):
            return True
    except Exception:
        pass
    try:
        return isinstance(await _require_cache_manager_permission(bot, {**(data or {}), "userId": str(user_id)}), int)
    except Exception:
        return False


async def cantusStatus(bot: commands.Bot, data: Dict, message: str = None, ok: bool = True) -> Dict:
    """Patch 46: the dashboard's Cantus section - whether this Discord user
    is connected, as whom, with how many songs, and the default server.
    Never includes the token (only cfa_…<last 4>)."""
    from voicelink import cantus
    user_id = int(data.get("userId"))
    try:
        state = await cantus.status(user_id)
    except Exception:
        state = {"connected": False, "defaultServer": cantus.base_url()}
    reply = {"op": "cantusStatus", "userId": str(user_id), **state, "canSetServer": await _is_bot_admin(bot, user_id, data)}
    if message:
        reply.update(message=message, ok=ok)
    return reply


async def cantusSave(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 46 / issue #2: the dashboard's Cantus form. `code` is what the
    person pasted - a connect code (`cantus+https://cfa_…@server`, its own
    server) or a bare `cfa_` token (the default server). The bot's owner,
    DJs and server admins can also set the default server (`server`). Pages
    loaded before issue #2 still send `server` + `token`; that keeps working."""
    from voicelink import cantus
    user_id = int(data.get("userId"))
    code = str(data.get("code") or "").strip()
    server = str(data.get("server") or "").strip()
    token = str(data.get("token") or "").strip()
    can_set_server = await _is_bot_admin(bot, user_id, data)
    done = []
    try:
        if can_set_server and server and cantus.normalize_server(server) != cantus.base_url():
            await cantus.save_default_server(server)
            done.append("default server saved for everyone")
    except cantus.CantusError as e:
        return await cantusStatus(bot, data, _cantus_message(e, server), ok=False)
    target = None
    try:
        if code:
            target = cantus.parse_connect_code(code)[0] or cantus.base_url()
            account, target = await cantus.connect_code(user_id, code)
            done.append(f"connected as {account.get('username') or '?'}")
        elif token:  # an old page: separate server + token fields
            target = (server if can_set_server and server else cantus.base_url()) or server
            if not target:
                return await cantusStatus(bot, data, _CANTUS_MESSAGES["no_default_server"], ok=False)
            account = await cantus.connect_token(user_id, target, token)
            done.append(f"connected as {account.get('username') or '?'}")
    except cantus.CantusError as e:
        prefix = "Default server saved, but connecting failed: " if done else ""
        return await cantusStatus(bot, data, prefix + _cantus_message(e, target), ok=False)
    if not done:
        return await cantusStatus(bot, data, "Paste your Cantus connect code first.", ok=False)
    text = "; ".join(done)
    return await cantusStatus(bot, data, text[0].upper() + text[1:] + ".")


def _cantus_song_payload(song: Dict, user_id: int, server: str) -> Dict:
    """Patch 46: a song for the dashboard's Cantus tab - its queueable track
    id (song id + metadata, never a play link) and what the row shows."""
    from voicelink import cantus
    track = cantus.build_track(song, user_id, None, server)
    return {
        "trackId": track.track_id,
        "title": song.get("title"), "artists": song.get("artists"), "album": song.get("album"),
        "durationMs": song.get("duration_ms"), "cover": song.get("cover"), "quality": song.get("quality"),
    }


async def _cantus_reply(op: str, data: Dict, call) -> Dict:
    """Runs a Cantus tab request for this user; not connected / Cantus errors
    come back as {"error": <message>, "notConnected": bool}."""
    from voicelink import cantus
    user_id = int(data.get("userId"))
    reply = {"op": op, "userId": str(user_id)}
    try:
        server = await cantus.server_of(user_id)
        reply.update(await call(user_id, server))
    except cantus.CantusError as e:
        reply.update(error=_cantus_message(e) if not e.not_linked else "Connect your Cantus account first (Settings -> Cantus).", notConnected=e.not_linked)
    return reply


async def cantusSongs(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 46: the Cantus tab's song list - your library, searched live,
    50 at a time (`offset` pages on)."""
    from voicelink import cantus
    query = str(data.get("query") or "")[:200]
    try:
        offset = max(0, int(data.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    async def call(user_id, server):
        result = await cantus.search_songs(user_id, query, limit=50, offset=offset)
        songs = [_cantus_song_payload(s, user_id, server) for s in result.get("songs", []) if s.get("id")]
        return {"songs": songs, "query": query, "append": offset > 0, "total": result.get("total"),
                "nextOffset": result.get("next_offset")}
    return await _cantus_reply("cantusSongs", data, call)


async def cantusPlaylists(bot: commands.Bot, data: Dict) -> Dict:
    from voicelink import cantus

    async def call(user_id, server):
        lists = await cantus.playlists(user_id)
        return {"playlists": [{"id": str(p["id"]), "name": p.get("name"), "songs": p.get("songs", 0)} for p in lists if p.get("id")]}
    return await _cantus_reply("cantusPlaylists", data, call)


async def cantusPlaylist(bot: commands.Bot, data: Dict) -> Dict:
    from voicelink import cantus
    playlist_id = str(data.get("playlistId") or "")

    async def call(user_id, server):
        p = await cantus.playlist(user_id, playlist_id)
        return {"id": playlist_id, "name": p.get("name"),
                "songs": [_cantus_song_payload(s, user_id, server) for s in p.get("songs", []) if s.get("id")]}
    return await _cantus_reply("cantusPlaylist", data, call)


async def cantusDisconnect(bot: commands.Bot, data: Dict) -> Dict:
    from voicelink import cantus
    await cantus.disconnect(int(data.get("userId")))
    return await cantusStatus(bot, data, "Disconnected - your token is forgotten.")


async def getFullHistory(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 45: the full history page, 100 songs at a time - each song once,
    at the last time it was played/added, with how many times - newest first.
    scope "user": everything this user played or added, in any server.
    scope "guild": every song played in the user's server (by anyone).
    `offset` (how many songs are already shown) pages further back."""
    import datetime as _dt
    user_id = int(data.get("userId"))
    scope = data.get("scope") if data.get("scope") in ("user", "guild") else "user"
    try:
        offset = max(0, int(data.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    reply = {"op": "getFullHistory", "userId": str(user_id), "scope": scope, "events": [], "done": True, "append": offset > 0}
    if scope == "guild":
        guild = _resolve_guild_for_user(bot, user_id, data)
        if not guild:
            return reply
        reply["guildName"] = guild.name
        query = {"guild_id": guild.id, "kind": "played", "private": {"$ne": True}}  # Patch 46
    else:
        query = {"user_id": user_id}
    songs = await MongoDBHandler.get_history_songs(query, offset, 100)
    names = {}
    for e in songs:
        gid = e.get("guild_id")
        if gid not in names:
            g = bot.get_guild(gid) if gid else None
            names[gid] = g.name if g else None
    reply["events"] = [{
        "trackId": e.get("track_id"),
        "kind": e.get("kind"),
        "at": (e["at"].replace(tzinfo=_dt.timezone.utc) if e["at"].tzinfo is None else e["at"]).isoformat(),
        "guildName": names.get(e.get("guild_id")),
        "plays": e.get("plays", 0),
        "adds": e.get("adds", 0),
    } for e in songs if e.get("track_id") and e.get("at")]
    reply["offset"] = offset + len(songs)
    reply["done"] = len(songs) < 100
    return reply

async def getGuildTopTracks(bot: commands.Bot, data: Dict) -> Dict:
    """Patch 9: powers the dashboard Explore page's "Trending" section.
    Previously this always fetched one hardcoded YouTube Music playlist
    regardless of server - now it's this guild's own most-played tracks,
    again split into "bot" (searched) vs "upload" (Discord-attachment)
    buckets, ranked by `Player._record_guild_play_history`'s running
    per-track play counts.
    """
    user_id = int(data.get("userId"))
    guild = _resolve_guild_for_user(bot, user_id, data)
    if not guild:
        return {"op": "getGuildTopTracks", "userId": str(user_id), "botTracks": [], "uploadTracks": []}

    settings = await MongoDBHandler.get_settings(guild.id)
    return {
        "op": "getGuildTopTracks",
        "userId": str(user_id),
        "guildId": str(guild.id),
        "botTracks": _top_tracks_from_counts(settings.get("botTopCounts", {})),
        "uploadTracks": _top_tracks_from_counts(settings.get("uploadTopCounts", {})),
    }

async def _require_cache_manager_permission(bot: commands.Bot, data: Dict) -> Union[Dict, int]:
    """Shared guard for getCacheEntries/deleteCacheEntry: DJ-or-admin, the
    same privilege level as every other playback control (skipTo,
    updateVolume, etc. - see Player.is_privileged()), not manage_guild
    alone, which would lock out DJs who aren't full server admins.

    The disk cache (voicelink/stream_cache.py) is bot-wide, not specific to
    any one guild, so this doesn't require the dashboard to have a specific
    guild selected (`data["guildId"]` is best-effort and can be missing/
    stale - e.g. right after opening the dashboard with no guild joined yet,
    which used to crash this with a bare `int(None)` TypeError). It tries
    that guild first, then falls back to every other guild the bot shares
    with this user, and accepts the first one where they're a recognized
    DJ/admin: if a player happens to be connected there, that's straight off
    its own is_privileged() (check_user_join=False - being in the voice
    channel is irrelevant to managing a disk cache); otherwise it replicates
    is_privileged()'s same bot_access_user/manage_guild/configured-DJ-role
    checks directly against the guild's stored settings, since there's no
    Player instance to ask.

    Returns the int user_id on success, or an error_msg dict to return
    as-is on failure.
    """
    user_id_raw = data.get("userId")
    if not user_id_raw:
        return error_msg("Missing user id - please refresh the dashboard and try again.", level="error")
    user_id = int(user_id_raw)

    candidate_guilds = []
    seen_guild_ids = set()
    try:
        preferred_guild_id = int(data.get("guildId"))
    except (TypeError, ValueError):
        preferred_guild_id = None
    if preferred_guild_id is not None and (guild := bot.get_guild(preferred_guild_id)):
        candidate_guilds.append(guild)
        seen_guild_ids.add(guild.id)
    candidate_guilds.extend(g for g in bot.guilds if g.id not in seen_guild_ids)

    is_member_of_any = False
    for guild in candidate_guilds:
        member = guild.get_member(user_id)
        if not member:
            continue
        is_member_of_any = True

        if player := guild.voice_client:
            privileged = await _is_privileged_fresh(player, member, check_user_join=False)
        elif member.id in Config().bot_access_user or member.guild_permissions.manage_guild:
            privileged = True
        else:
            settings = await MongoDBHandler.get_settings(guild.id)
            dj_role_id = settings.get("dj")
            privileged = bool(dj_role_id and dj_role_id in [role.id for role in member.roles])

        if privileged:
            return user_id

    if not is_member_of_any:
        return error_msg("Vocard doesn't share a server with you.", user_id=user_id, level="error")
    return error_msg("Only the DJ or admins may manage the audio cache.", user_id=user_id, level="error")

async def getCacheEntries(bot: commands.Bot, data: Dict) -> Dict:
    """Lists every entry in the yt-dlp disk cache for the dashboard's cache
    manager - size, view count, ready/downloading/failed state, and whether
    it's current/upcoming in any connected player's queue right now. See
    docs/local-patches.md for the full feature.
    """
    user_id = await _require_cache_manager_permission(bot, data)
    if not isinstance(user_id, int):
        return user_id

    from voicelink import stream_cache
    entries = await stream_cache.list_entries()
    stats = await stream_cache.get_stats()

    return {
        "op": "cacheEntries",
        "entries": entries,
        "totalBytes": stats["totalBytes"],
        "maxBytes": stats["maxBytes"],
        "userId": str(user_id)
    }

async def deleteCacheEntry(bot: commands.Bot, data: Dict) -> Dict:
    """Manually evicts one entry from the disk cache. Unlike StreamCache's
    own automatic eviction (_prune(), triggered only under space pressure),
    this is an explicit admin action and bypasses the "don't delete
    something actively queued" protection - the user's own call to make,
    same zero-risk-to-playback guarantee as everywhere else in this feature
    if they do delete something still in use (it just falls back to an
    on-demand resolve next time). Also how a stuck/failed entry gets cleared
    to force an immediate retry instead of waiting out the retry cooldown.
    """
    user_id = await _require_cache_manager_permission(bot, data)
    if not isinstance(user_id, int):
        return user_id

    video_id = data.get("videoId")
    if not video_id:
        return error_msg("Missing videoId.", user_id=user_id, level="error")

    from voicelink import stream_cache
    deleted = await stream_cache.delete_entry(video_id)
    entries = await stream_cache.list_entries()
    stats = await stream_cache.get_stats()

    return {
        "op": "cacheEntries",
        "entries": entries,
        "totalBytes": stats["totalBytes"],
        "maxBytes": stats["maxBytes"],
        "deleted": deleted,
        "userId": str(user_id)
    }

async def getLyrics(bot: commands.Bot, data: Dict) -> Dict:
    title, artist, platform = data.get("title", ""), data.get("artist", ""), data.get("platform", "")
    if not platform or platform not in LYRICS_PLATFORMS:
        platform = Config().lyrics_platform
    
    lyrics_platform = LYRICS_PLATFORMS.get(platform)
    if lyrics_platform:
        lyrics: dict[str, str] = await lyrics_platform().get_lyrics(title, artist)
        return {
            "op": "getLyrics",
            "userId": data.get("userId"),
            "title": title,
            "artist": artist,
            "platform": platform,
            "lyrics": {_: re.findall(r'.*\n(?:.*\n){,22}', v or "") for _, v in lyrics.items()} if lyrics else {},
            "callback": data.get("callback")
        }

async def updateSettings(bot: commands.Bot, data: Dict) -> None:
    user_id = int(data.get("userId"))
    guild_id  = int(data.get("guildId"))

    guild = bot.get_guild(guild_id)
    if not guild:
        return error_msg("Vocard don't have access to required guild.", user_id=user_id, level="error")

    member = guild.get_member(user_id)
    if not member:
        return error_msg("You are not in the required guild.", user_id=user_id, level="error")
    
    if not member.guild_permissions.manage_guild:
        return error_msg("You don't have permission to change the settings.", user_id=user_id, level='error')
    
    data = data.get("settings", {})
    if "dj" in data:
        for role in guild.roles:
            if role.name.lower() == data["dj"]:
                data["dj"] = role.id
                break

    for key, value in data.copy().items():
        if key not in SCOPES or not isinstance(value, SCOPES[key]):
            del data[key]

    await MongoDBHandler.update_settings(guild.id, {"$set": data})

METHODS: Dict[str, Union[SystemMethod, PlayerMethod]] = {
    "initBot": SystemMethod(initBot, credit=0),
    "initUser": SystemMethod(initUser, credit=2),
    "getPlaylist": SystemMethod(getPlaylist),
    "updatePlaylist": SystemMethod(updatePlaylist, credit=2),
    "getMutualGuilds": SystemMethod(getMutualGuilds),
    "getSettings": SystemMethod(getSettings),
    "getCacheEntries": SystemMethod(getCacheEntries),
    "deleteCacheEntry": SystemMethod(deleteCacheEntry, credit=2),
    "getLyrics": SystemMethod(getLyrics),
    "updateSettings": SystemMethod(updateSettings),
    "getRecommendation": SystemMethod(getRecommendation, credit=5),
    "closeConnection": SystemMethod(closeConnection, credit=0),
    "getTracks": SystemMethod(getTracks, credit=5),
    "getGuildHistory": SystemMethod(getGuildHistory, credit=2),
    "getGuildAddedHistory": SystemMethod(getGuildAddedHistory, credit=2),
    "getUserHistory": SystemMethod(getUserHistory, credit=2),
    "getFullHistory": SystemMethod(getFullHistory, credit=2),
    "cantusStatus": SystemMethod(cantusStatus, credit=2),
    "cantusDisconnect": SystemMethod(cantusDisconnect, credit=5),
    "cantusSave": SystemMethod(cantusSave, credit=10),
    "cantusSongs": SystemMethod(cantusSongs, credit=2),
    "cantusPlaylists": SystemMethod(cantusPlaylists, credit=2),
    "cantusPlaylist": SystemMethod(cantusPlaylist, credit=2),
    "getGuildTopTracks": SystemMethod(getGuildTopTracks, credit=2),
    "initPlayer": PlayerMethod(initPlayer),
    "skipTo": PlayerMethod(skipTo),
    "backTo": PlayerMethod(backTo),
    "moveTrack": PlayerMethod(moveTrack),
    "addTracks": PlayerMethod(addTracks, auto_connect=True),
    "shuffleTrack": PlayerMethod(shuffleTrack, credit=3),
    "repeatTrack": PlayerMethod(repeatTrack),
    "removeTrack": PlayerMethod(removeTrack),
    "clearQueue": PlayerMethod(clearQueue),
    "updateVolume": PlayerMethod(updateVolume, credit=2),
    "updatePause": PlayerMethod(updatePause),
    "updatePosition": PlayerMethod(updatePosition),
    "toggleAutoplay": PlayerMethod(toggleAutoplay),
    "updateFilter": PlayerMethod(updateFilter),
    "searchAndPlay": PlayerMethod(searchAndPlay, credit=5, auto_connect=True)
}

async def process_methods(ipc_client: IPCClient, bot: commands.Bot, data: Dict) -> None:
    op: str = data.get("op", "")
    method = METHODS.get(op)
    if not method or not (user_id := data.get("userId")):
        return

    user_id = int(user_id)
    if user_id not in RATELIMIT_COUNTER or (time.time() - RATELIMIT_COUNTER[user_id]["time"]) >= 300:
        RATELIMIT_COUNTER[user_id] = {"time": time.time(), "count": 0}
    
    else:
        # Patch 43: 600 (was 100) credits per 5 minutes - normal use, even with
        # several tabs open, never gets near it; still stops a runaway page.
        if RATELIMIT_COUNTER[user_id]["count"] >= 600:
            return await ipc_client.send({"op": "rateLimited", "userId": str(user_id)})
        RATELIMIT_COUNTER[user_id]["count"] += method.credit

    try:
        env: Dict = {"bot": bot, "data": data}
        args: List = []
        
        params = method.params
        if not (type(method) == SystemMethod):
            if guild_id := data.get("guildId"):
                if (guild := bot.get_guild(int(guild_id))):
                    env["guild"] = guild

            else:
                # Diagnostic note (2026-10-18): bot.get_user()/guild.get_member()
                # are pure in-memory cache reads, which main.py deliberately
                # never pre-fills via chunk_guilds_at_startup=False (avoids the
                # chunking cost/rate-limit hit of eagerly pulling every member
                # of every guild on a bot that may be in many servers). That
                # cache instead fills in opportunistically as discord.py
                # observes the user through other gateway events - which,
                # for a *newly*-invited bot and a member who hasn't yet
                # triggered one of those (sent a message, run a command,
                # etc.), may simply never have happened yet. A plain
                # voice-channel join doesn't reliably backfill it either for
                # a member discord.py has never seen before. Without an API
                # fallback, this silently drops every PlayerMethod call
                # (initPlayer, addTracks, ...) for that member - indistinguishable
                # from "not in a voice channel" from every log and from the
                # dashboard - until they happen to trigger some other cached
                # event. bot.fetch_user()/guild.fetch_member() hit the REST
                # API directly instead of the cache, so they work immediately
                # regardless of chunking state; only used as a fallback (not
                # the default) so the common, already-cached case costs
                # nothing extra.
                user: Optional[User] = bot.get_user(user_id)
                if not user:
                    try:
                        user = await bot.fetch_user(user_id)
                    except (NotFound, HTTPException):
                        user = None
                if not user:
                    logger.debug(f"process_methods({op}): user {user_id} not found via cache or fetch_user - dropping request")
                    return

                for guild in bot.guilds:
                    member = guild.get_member(user_id)
                    if not member:
                        try:
                            member = await guild.fetch_member(user_id)
                        except (NotFound, HTTPException):
                            continue
                    if member.voice and member.voice.channel:
                        env["guild"] = guild
                        env["member"] = member
                        break

                if "guild" not in env:
                    logger.debug(f"process_methods({op}): user {user_id} is not in a voice channel in any guild this bot is in - dropping request")

            if "member" in params and "member" not in env:
                if not (guild := env.get("guild")) or not (member := guild.get_member(user_id)):
                    return
                env["member"] = member
                
            if "player" in params:
                if not (guild := env.get("guild")) or not (player := guild.voice_client):
                    if not method.auto_connect or not (member := env.get("member")):
                        return
                    player = await connect_channel(member, bot)

                if not player or player.channel.id != member.voice.channel.id:
                    return
                
                env["player"] = player
        
        for param in params:
            args.append(env.get(param))
            
        if resp := await method.function(*args):
            await ipc_client.send(resp)

    except Exception as e:
        ipc_client._logger.error(f"Failed to process method '{op}' by user {user_id}", exc_info=e)
        payload = {
            "op": "errorMsg",
            "level": "error",
            "msg": str(e),
            "userId": str(user_id)
        }
        await ipc_client.send(payload)