"""The hub's own settings that admins change on the website (/admin's "Hub settings"), kept in its database (the
`settings` table) and in effect as soon as they're saved. What the hub needs before it can serve a page (its data
folder, database, address, and port, and its voice clips' folder) is in `~/.burble/config.toml` instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HubSetting:
    key: str  # also its key in the `settings` table
    label: str
    help: str
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label); none: free text
    url: bool = False  # an http(s) address (or empty)


HUB_SETTINGS = (
    HubSetting("pilot_hook_accept", "Landings from pilot hooks",
               "The traps pilots' own pilot hooks send. \"Any server\" takes them from other communities' servers "
               "too, from a pilot with a pilot token from this hub. Without a token, a pilot's traps are only taken "
               "while they're on one of this hub's servers, either way.", "ours",
               (("ours", "Only this hub's servers"), ("any", "Any server, with a pilot token"))),
    HubSetting("require_upload_token", "Recordings uploaded by hand",
               "Tacview recordings uploaded on the Upload page or with burble upload. From anyone, each uploader "
               "imports their own passes from it; a token (a server agent's or a pilot token) imports every pass in "
               "it. Server agents always need their own token.", "",
               (("", "From anyone"), ("on", "Only with a token"))),
    HubSetting("other_servers", "Landings flown on other servers",
               "Traps pilots' hooks sent from other communities' servers. Hidden, they're on the board only when a "
               "viewer picks \"All servers\", and aren't posted to Discord.", "shown",
               (("shown", "On the greenie board"), ("hidden", "Hidden"))),
    HubSetting("public_url", "The hub's address",
               "As people reach it, e.g. https://lso.example.com: for the links in Discord posts.", url=True),
    HubSetting("discord_board_webhook", "The greenie board",
               "A webhook (Server Settings > Integrations > Webhooks in Discord) for the board: one message, kept up "
               "to date by editing it. Empty: not posted.", url=True),
    HubSetting("discord_traps_webhook", "Each landing",
               "A webhook for a post per landing, with its trap card (may be the same one as the board's). Empty: "
               "not posted.", url=True),
    HubSetting("default_voice", "Default voice",
               "The voice (clip set) the live view and replays say calls in when a call doesn't say which voice made "
               "it (recorded before 2026-10-27), or names one this hub hasn't got. Empty: the first by name."),
)
BY_KEY = {s.key: s for s in HUB_SETTINGS}


def checked(key: str, value: str) -> str:
    """`value` for the setting `key`, tidied, or ValueError saying what's wrong with it."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise ValueError(f"no hub setting {key!r}")
    value = (value or "").strip()
    if setting.choices and value not in [v for v, _ in setting.choices]:
        raise ValueError(f"{setting.label}: should be one of {', '.join(repr(v) for v, _ in setting.choices)}")
    if setting.url and value:
        if not value.startswith(("https://", "http://")) or any(c.isspace() for c in value):
            raise ValueError(f"{setting.label}: should be an address starting https:// (it's {value!r})")
        value = value.rstrip("/")
    if len(value) > 500:
        raise ValueError(f"{setting.label}: too long")
    return value
