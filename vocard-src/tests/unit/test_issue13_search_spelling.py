"""Issue #13: a typed YouTube search that finds nothing because of a spelling
mistake is retried once with YouTube's own correction ("Did you mean")."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from voicelink.enums import SearchType
from voicelink.pool import Node, parse_corrected_query

PAGE = (
    'xx"didYouMeanRenderer":{"correctedQuery":{"runs":[{"text":"three "},'
    '{"text":"choppers","italics":true},{"text":" over "},'
    '{"text":"mozdok","italics":true}]},"correctedQueryEndpoint":{}}xx'
)

SONG = {"encoded": "abc", "info": {"identifier": "x", "title": "Three choppers over Mozdok",
                                   "author": "a", "length": 1000, "uri": "https://youtu.be/x",
                                   "isStream": False, "isSeekable": True, "sourceName": "youtube",
                                   "position": 0}}


def test_parse_corrected_query_joins_runs():
    assert parse_corrected_query(PAGE) == "three choppers over mozdok"


@pytest.mark.parametrize("page", ["", "<html>no correction</html>", '"correctedQuery":{"runs":[broken]}'])
def test_parse_corrected_query_none(page):
    assert parse_corrected_query(page) is None


@pytest.fixture(autouse=True)
def plain_tracks(monkeypatch):
    # Track() reads the bot config; only the parsed title matters here.
    monkeypatch.setattr("voicelink.pool.Track", lambda track_id, info, requester: SimpleNamespace(title=info["title"]))


def make_node(responses, corrected="three choppers over mozdok"):
    node = Node.__new__(Node)
    node._logger = MagicMock()
    node.send = AsyncMock(side_effect=responses)
    node.youtube_corrected_query = AsyncMock(return_value=corrected)
    return node


async def test_empty_search_retries_with_correction():
    node = make_node([{"loadType": "empty", "data": {}}, {"loadType": "search", "data": [SONG]}])
    tracks = await node.get_tracks("ytsearch:three coppers over mozdoz", requester=None, search_type=SearchType.YOUTUBE)
    assert [t.title for t in tracks] == ["Three choppers over Mozdok"]
    node.youtube_corrected_query.assert_awaited_once_with("three coppers over mozdoz")
    assert "ytsearch%3Athree%20choppers%20over%20mozdok" in node.send.await_args_list[1].args[1]


async def test_retries_only_once():
    node = make_node([{"loadType": "empty", "data": {}}, {"loadType": "empty", "data": {}}])
    assert await node.get_tracks("ytsearch:aaa", requester=None, search_type=SearchType.YOUTUBE) is None
    assert node.send.await_count == 2
    node.youtube_corrected_query.assert_awaited_once()


async def test_no_correction_returns_none():
    node = make_node([{"loadType": "empty", "data": {}}], corrected=None)
    assert await node.get_tracks("ytsearch:zzz", requester=None, search_type=SearchType.YOUTUBE) is None
    assert node.send.await_count == 1


async def test_same_text_correction_is_not_retried():
    node = make_node([{"loadType": "empty", "data": {}}], corrected="ZZZ")
    assert await node.get_tracks("ytsearch:zzz", requester=None, search_type=SearchType.YOUTUBE) is None
    assert node.send.await_count == 1


@pytest.mark.parametrize("query", ["https://www.youtube.com/watch?v=x", "scsearch:something", "spsearch:x"])
async def test_links_and_other_sources_not_retried(query):
    node = make_node([{"loadType": "empty", "data": {}}])
    assert await node.get_tracks(query, requester=None, search_type=SearchType.YOUTUBE) is None
    node.youtube_corrected_query.assert_not_awaited()


async def test_ytmsearch_keeps_its_prefix():
    node = make_node([{"loadType": "empty", "data": {}}, {"loadType": "search", "data": [SONG]}])
    await node.get_tracks("ytmsearch:three coppers over mozdoz", requester=None, search_type=SearchType.YOUTUBE)
    assert "ytmsearch%3Athree%20choppers" in node.send.await_args_list[1].args[1]


async def test_lookup_failure_is_quiet():
    node = Node.__new__(Node)
    node._logger = MagicMock()
    node._session = MagicMock(get=MagicMock(side_effect=OSError("down")))
    assert await node.youtube_corrected_query("x") is None
    node._logger.warning.assert_called_once()
