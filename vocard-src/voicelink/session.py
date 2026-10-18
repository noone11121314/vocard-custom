"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

Patch 18 (2026-10-18): local addition, not part of upstream Vocard.

Saves every guild's current player (current track + upcoming queue +
repeat/volume/autoplay) to a JSON file so cogs/listeners.py's
restore_last_session_players() can bring it all back on the next startup -
see docs/local-patches.md's Patch 18.

A standalone module (rather than living in cogs/listeners.py, where the
restore side already lived) specifically so both `cogs/listeners.py`
(outside the voicelink package, imports it to call from main.py's
graceful-shutdown signal handler) and `voicelink/views/debug.py` (inside
the package, the pre-existing "Stop-Bot" debug button) can import it
without creating a cycle: this module only ever imports *from* `.pool`/
`.config`, never the other way around.
"""

from __future__ import annotations

import json
import logging

from .pool import NodePool
from .config import Config

logger = logging.getLogger("vocard")


def save_last_session() -> None:
    """Dumps every guild's player that currently has something playing or
    queued to Config.LAST_SESSION_FILE_DIR. Synchronous and fast
    (Player.data is plain attribute reads, no I/O) - meant to be callable
    directly from a signal handler, which needs to finish well within
    Docker's SIGTERM grace period before SIGKILL.

    Fixes a condition bug in the original "Stop-Bot" debug button (the
    only place this logic used to live): it skipped - did NOT save -
    exactly the guilds where `player.guild.me.voice` was truthy, i.e. the
    ones that actually had a live voice connection, and only saved players
    whose voice state already looked stale/empty. Saves whenever a player
    has a current track or a non-empty queue, regardless of that check.
    """
    player_data = []
    for node in NodePool._nodes.values():
        for player in node._players.copy().values():
            if not player.current and player.queue.is_empty:
                continue
            try:
                player_data.append(player.data)
            except Exception as e:
                logger.error(
                    f"Failed to collect session data for a player in {getattr(player, 'guild', None)}.",
                    exc_info=e
                )

    try:
        with open(Config.LAST_SESSION_FILE_DIR, "w", encoding="utf8") as f:
            json.dump(player_data, f, ensure_ascii=False, indent=4)
        logger.info(f"Saved last-session data for {len(player_data)} guild(s).")
    except Exception as e:
        logger.error("Failed to write last-session file.", exc_info=e)
