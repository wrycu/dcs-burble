"""Discord: a post per landing (edited when a better report merges in) and the greenie board as one message kept
up to date by editing it, against a stand-in for Discord's webhook API."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dcs_lso.hub.db import Pass, Setting
from dcs_lso.hub.discord import BOARD_SETTING, Discord
from dcs_lso.hub.service import Hub
from test_pilot_hook import HOME, client_at, hook_upload, post, server_report

TRAPS = "https://discord.test/api/webhooks/1/traps"
BOARD = "https://discord.test/api/webhooks/2/board"


class FakeDiscord:
    """Records requests; posts get new message ids; edits of unknown messages are 404."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.messages: set[str] = set()
        self.rate_limit_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, json={"retry_after": 0.01})
        body = request.content
        if request.headers.get("content-type", "").startswith("multipart/"):
            payload = json.loads(body.split(b'name="payload_json"\r\n\r\n')[1].split(b"\r\n--")[0])
            image = body.split(b"\x89PNG")[1][:4] if b"\x89PNG" in body else None
        else:
            payload, image = json.loads(body), None
        url = str(request.url)
        self.requests.append((request.method, url, payload, image))
        if request.method == "PATCH":
            message = url.rsplit("/", 1)[1]
            return httpx.Response(200 if message in self.messages else 404, json={"id": message})
        message = str(100 + len(self.messages))
        self.messages.add(message)
        return httpx.Response(200, json={"id": message})


@pytest.fixture
def setup(tmp_path):
    from test_pilot_hook import make_hub
    hub = make_hub(tmp_path)
    fake = FakeDiscord()
    discord = Discord(hub, BOARD, TRAPS, "https://lso.example.com",
                      client=httpx.Client(transport=httpx.MockTransport(fake)), start=False)
    return hub, discord, fake


def fresh_report() -> tuple[bytes, dict]:
    data, meta = server_report()
    meta["pass"]["occurred_at"] = datetime.now(UTC).isoformat()  # flown just now
    return data, meta


def test_a_landing_is_posted_and_edited_when_a_better_report_merges(setup):
    hub, discord, fake = setup
    result = hub.ingest(1, *fresh_report())
    discord.step(result.pass_id, now=100.0)
    (method, url, payload, image), board = fake.requests
    assert (method, url) == ("POST", TRAPS + "?wait=true") and image is not None  # with the trap card as a PNG
    embed = payload["embeds"][0]
    assert embed["title"].startswith("Wrycu · ") and embed["url"] == f"https://lso.example.com/passes/{result.pass_id}"
    assert embed["image"]["url"] == "attachment://card.png" and "trap" in embed["description"]
    assert board[0] == "POST" and board[2]["embeds"] == [] and board[3]  # the image, no embed (no coloured bar)
    assert board[2]["content"] == "**[Greenie Board](<https://lso.example.com/>)**"
    with hub.sessions() as s:
        message_id = s.get(Pass, result.pass_id).discord_message_id
        assert message_id and s.get(Setting, BOARD_SETTING).value
    # The pilot hook's report merges in (better track): the same post is edited, not a new one.
    fake.requests.clear()
    (report,) = post(client_at(hub, HOME), hook_upload(), None).json()["reports"]
    discord.step(report["pass_id"], now=200.0)
    methods = [(m, u) for m, u, _, _ in fake.requests]
    assert ("PATCH", f"{TRAPS}/messages/{message_id}") in methods and ("POST", TRAPS + "?wait=true") not in methods
    assert any(m == "PATCH" and u.startswith(BOARD) for m, u in methods)


def test_the_board_is_edited_in_place_and_reposted_if_deleted(setup):
    hub, discord, fake = setup
    discord.step(None, now=0.0)
    discord.step(None, now=5.0)  # too soon: at most every 10 s
    discord.step(None, now=20.0)
    assert [m for m, u, _, _ in fake.requests] == ["POST", "PATCH"]
    fake.messages.clear()  # someone deleted the message in Discord
    discord.step(None, now=40.0)
    assert [m for m, u, _, _ in fake.requests][-2:] == ["PATCH", "POST"]


def test_old_landings_are_not_posted_one_by_one(setup):
    hub, discord, fake = setup
    data, meta = server_report()
    meta["pass"]["occurred_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()  # e.g. an old recording
    result = hub.ingest(1, data, meta)
    discord.step(result.pass_id, now=0.0)
    assert [u for _, u, _, _ in fake.requests] == [BOARD + "?wait=true"]  # only the board
    assert [r.name for r in discord.board_rows()] == ["Wrycu"]


def test_rate_limits_are_waited_out(setup):
    hub, discord, fake = setup
    fake.rate_limit_once = True
    discord.step(None, now=0.0)
    assert [m for m, _, _, _ in fake.requests] == ["POST"]


def test_ingest_tells_discord(tmp_path):
    hub = Hub(f"sqlite:///{tmp_path / 'lso.db'}", tmp_path / "hub")
    hub.add_source("server1")
    discord = Discord(hub, BOARD, None, client=httpx.Client(transport=httpx.MockTransport(FakeDiscord())), start=False)
    result = hub.ingest(1, *fresh_report())
    assert discord._queue.get_nowait() == result.pass_id


def test_board_image():
    from dcs_lso.cards.board import BoardRow, render_board
    import xml.etree.ElementTree as ET
    rows = [BoardRow("Wrycu", 3, 3.17, 2 / 3, [("OK", False), ("B", True), ("(OK)", False)]),
            BoardRow("Goose & <Maverick>", 1, None, 0.0, [("WO", False)])]
    svg = render_board(rows, 15, subtitle="last 30 days")
    ET.fromstring(svg)  # well-formed, names escaped
    assert "Wrycu" in svg and "Goose &amp; &lt;Maverick&gt;" in svg and "3.17" in svg and "67%" in svg
    assert svg.count("<circle") == 2  # one night landing, plus the legend's
    assert "No passes yet" in render_board([], 15)
    from dcs_lso.hub.discord import png
    assert png(svg).startswith(b"\x89PNG")


def test_a_regrade_from_the_command_line_reaches_discord(setup):
    hub, discord, fake = setup
    result = hub.ingest(1, *fresh_report())
    discord.step(result.pass_id, now=0.0)
    with hub.sessions() as s:
        message_id = s.get(Pass, result.pass_id).discord_message_id
    assert discord.pick_up_changes() == []
    hub.regrade(force=True)  # nothing changed: nothing to do
    assert discord.pick_up_changes() == []
    with hub.sessions.begin() as s:
        s.get(Pass, result.pass_id).outcome = "bolter"  # e.g. stored before a detection fix
    hub.regrade(force=True)  # another process: puts the outcome right and marks the landing
    fake.requests.clear()
    assert discord.pick_up_changes() == [result.pass_id] and discord.pick_up_changes() == []
    discord.step(discord._queue.get_nowait(), now=100.0)
    methods = [(m, u) for m, u, _, _ in fake.requests]
    assert ("PATCH", f"{TRAPS}/messages/{message_id}") in methods and any(u.startswith(BOARD) for _, u in methods)
