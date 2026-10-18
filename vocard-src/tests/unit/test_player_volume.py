"""Unit tests for Patch 27 (volume sync): Player.set_volume saving the
guild volume from every path (except mute), play() restating the volume on
every track start, and the dashboard updateVolume clamp.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from voicelink.ipc.methods import updateVolume
from voicelink.player import Player

from tests.unit.test_player_loudness_gain import make_bare_player as make_play_ready_player
from tests.conftest import make_track


def make_volume_player() -> Player:
    player = Player.__new__(Player)
    player._logger = MagicMock()
    player._guild = MagicMock(id=555)
    player.send = AsyncMock()
    player.send_ws = AsyncMock()
    player._ipc_client = MagicMock(_is_connected=False)
    player._ipc_connection = False
    player._volume = 100
    player.settings = {"volume": 100}
    player._bot = MagicMock()
    player._bot.loop.create_task = MagicMock(side_effect=lambda coro: coro.close())
    return player


async def test_set_volume_saves_it_as_the_guild_volume(monkeypatch):
    player = make_volume_player()
    update_settings = MagicMock(return_value=AsyncMock()())
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)

    await player.set_volume(70)

    player.send.assert_awaited_once()
    assert player.send.call_args.kwargs["data"] == {"volume": 70}
    assert player.volume == 70
    assert player.settings["volume"] == 70
    update_settings.assert_called_once_with(555, {"$set": {"volume": 70}})


async def test_set_volume_clamps_to_the_shared_0_to_100_range(monkeypatch):
    """Patch 28: one shared bot volume, 0-100 (what the slider shows), no
    matter which path sets it."""
    player = make_volume_player()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", MagicMock(return_value=AsyncMock()()))

    assert await player.set_volume(140) == 100
    assert player.send.call_args.kwargs["data"] == {"volume": 100}
    assert await player.set_volume(-3) == 0


async def test_mute_does_not_overwrite_the_saved_volume(monkeypatch):
    """The Discord mute button passes persist=False so unmute can restore
    settings["volume"] - saving 0 there would make unmute restore 0."""
    player = make_volume_player()
    player.settings["volume"] = 60
    update_settings = MagicMock()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)

    await player.set_volume(0, persist=False)

    assert player.volume == 0
    assert player.settings["volume"] == 60
    update_settings.assert_not_called()


async def test_play_restates_the_players_volume_on_every_track_start():
    player = make_play_ready_player()
    player._volume = 24

    await player.play(make_track(source="soundcloud", identifier=None, track_id="ENC"))

    assert player.send.call_args.kwargs["data"]["volume"] == 24


async def test_dashboard_volume_is_clamped_to_0_to_100():
    player = MagicMock()
    player.set_volume = AsyncMock()

    await updateVolume(player, MagicMock(), {"volume": 900})
    assert player.set_volume.call_args.kwargs["volume"] == 100

    await updateVolume(player, MagicMock(), {"volume": -5})
    assert player.set_volume.call_args.kwargs["volume"] == 0


async def test_dashboard_volume_ignores_a_non_numeric_value():
    player = MagicMock()
    player.set_volume = AsyncMock()

    await updateVolume(player, MagicMock(), {"volume": "loud"})

    player.set_volume.assert_not_awaited()


# --- Patch 42: mute/unmute from the dashboard's speaker icon


async def test_mute_sets_zero_without_saving_it():
    player = MagicMock(settings={"volume": 60})
    player.set_volume = AsyncMock()

    assert await updateVolume(player, MagicMock(), {"mute": True, "volume": 0}) is None

    player.set_volume.assert_awaited_once()
    assert player.set_volume.call_args.args[0] == 0
    assert player.set_volume.call_args.kwargs["persist"] is False


async def test_unmute_restores_the_saved_volume():
    player = MagicMock(settings={"volume": 60})
    player.set_volume = AsyncMock()

    await updateVolume(player, MagicMock(), {"mute": False, "volume": 25})

    assert player.set_volume.call_args.args[0] == 60  # the saved level, not the page's guess


async def test_unmute_falls_back_when_the_saved_volume_is_zero():
    player = MagicMock(settings={"volume": 0})
    player.set_volume = AsyncMock()

    await updateVolume(player, MagicMock(), {"mute": False, "volume": 25})
    assert player.set_volume.call_args.args[0] == 25

    await updateVolume(player, MagicMock(), {"mute": False})
    assert player.set_volume.call_args.args[0] == 50
