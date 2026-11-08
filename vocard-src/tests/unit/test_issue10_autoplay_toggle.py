"""Issue #10: autoplay is an on/off toggle button on the Discord controller.
Every toggle (button, /autoplay, dashboard) goes through Player.set_autoplay,
so the button's look always matches the real setting."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from voicelink.ipc import methods
from voicelink.player import Player
from voicelink.views import controller as ctl

STATES = {
    "states": {
        "on": {"emoji": "📻", "label": "Autoplay", "style": "green"},
        "off": {"emoji": "📻", "label": "Autoplay"},
    }
}


def make_player(autoplay=False, playing=True, controller=True, privileged=True) -> Player:
    player = Player.__new__(Player)
    player.settings = {"autoplay": autoplay}
    player._is_connected = True
    player._current = MagicMock() if playing else None
    player._updating = False
    player.controller = MagicMock(edit=AsyncMock()) if controller else None
    player.do_next = AsyncMock()
    player._ph = MagicMock(replace=lambda text, variables: text)
    player.is_privileged = lambda member, check_user_join=True: privileged
    player._ipc_client = MagicMock(_is_connected=True)
    player._ipc_connection = True
    player.send_ws = AsyncMock()
    return player


async def test_set_autoplay_refreshes_controller_and_keeps_playing_song():
    player = make_player(autoplay=False, playing=True)

    with patch("voicelink.player.InteractiveController", return_value="view") as view:
        await player.set_autoplay(True)

    assert player.settings["autoplay"] is True
    view.assert_called_once_with(player)
    player.controller.edit.assert_awaited_once_with(view="view")
    player.do_next.assert_not_awaited()


async def test_set_autoplay_starts_a_song_when_idle():
    player = make_player(playing=False)

    with patch("voicelink.player.InteractiveController"):
        await player.set_autoplay(True)

    player.do_next.assert_awaited_once()


async def test_refresh_is_skipped_without_controller_and_survives_deleted_message():
    player = make_player(controller=False)
    await player.refresh_controller_view()  # no error

    player = make_player()
    player.controller.edit.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    with patch("voicelink.player.InteractiveController"):
        await player.refresh_controller_view()  # swallowed


@pytest.mark.parametrize("autoplay, style", [(True, discord.ButtonStyle.green), (False, discord.ButtonStyle.gray)])
async def test_button_shows_current_state(autoplay, style):
    button = ctl.AutoPlay(player=make_player(autoplay=autoplay), btn_data=STATES)

    assert button.style == style
    assert button.label == "Autoplay"


async def test_button_without_states_config_keeps_static_look():
    button = ctl.AutoPlay(player=make_player(), btn_data={"emoji": "🔁", "label": "Auto"})

    assert button.label == "Auto"


def interaction():
    inter = MagicMock()
    inter.response.edit_message = AsyncMock()
    return inter


async def test_dj_click_flips_button_tells_dashboard_and_starts_if_idle():
    player = make_player(autoplay=False, playing=False)
    button = ctl.AutoPlay(player=player, btn_data=STATES)
    button.send = AsyncMock()
    inter = interaction()

    with patch.object(ctl.LangHandler, "get_lang", AsyncMock(return_value="enabled")):
        await button.callback(inter)

    assert player.settings["autoplay"] is True
    assert button.style == discord.ButtonStyle.green
    inter.response.edit_message.assert_awaited_once()
    player.send_ws.assert_awaited_once_with({"op": "toggleAutoplay", "status": True})
    player.do_next.assert_awaited_once()


async def test_non_dj_click_is_refused():
    player = make_player(autoplay=False, privileged=False)
    button = ctl.AutoPlay(player=player, btn_data=STATES)
    button.send = AsyncMock()
    inter = interaction()

    await button.callback(inter)

    assert player.settings["autoplay"] is False
    inter.response.edit_message.assert_not_awaited()
    assert button.send.await_args.args[1] == "permissions.missingAutoPlay"


async def test_dashboard_toggle_goes_through_set_autoplay():
    player = make_player()
    player.set_autoplay = AsyncMock()
    player._guild = MagicMock(id=1)
    member = MagicMock(id=2)

    result = await methods.toggleAutoplay(player, member, {"status": True})

    player.set_autoplay.assert_awaited_once_with(True)
    assert result["status"] is True
