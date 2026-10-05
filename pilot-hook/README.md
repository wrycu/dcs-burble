# dcs-lso pilot hook

Sends your carrier approaches, recorded from your own jet, to your communities' LSO hubs. Your own jet's
data is better than what the server sees: true AOA and about 50 samples a second, so the hub can grade you
more precisely and estimate the wire. Nothing else to install: it runs inside DCS.

## Install

Copy the contents of this folder into your DCS Saved Games folder (`Saved Games/DCS`, or `DCS.openbeta`):

```
Mods/Services/DCS-LSO/              the settings page (Options > Special > DCS-LSO)
Scripts/Hooks/dcs-lso-pilot-hook.lua the uploader
Scripts/dcs-lso-pilot-recorder.lua   the recorder
```

Then add this line at the **end** of `Saved Games/DCS/Scripts/Export.lua` (create the file if you don't have
one; keep any Tacview, SRS or other lines above it):

```lua
pcall(function() dofile(lfs.writedir() .. [[Scripts\dcs-lso-pilot-recorder.lua]]) end)
```

## Set up

In DCS: **Options > Special > DCS-LSO**.

- **Hub address:** your community's LSO website, e.g. `lso.example.com`. Up to three hubs.
- **Pilot token:** only needed for "send to all hubs". Create one on your settings page on that hub (your
  pilot page, then Settings).
- **Send my traps to all hubs:** off (the default) sends each trap only to the hub of the server you're
  flying on. That hub recognises you without a token. On, it sends every trap to every hub listed, which
  needs a token for the hubs whose servers you weren't on.

## What happens

- After each approach to deck height, the recorder writes it to `Saved Games/DCS/Logs/dcs-lso/`.
- Where the server lets clients see other objects, the nearest carrier is recorded too. Then any hub can grade
  the pass on its own, even one whose community doesn't run that server.
- The uploader sends it in the background, while you fly and in the menus after the mission. Sent files move
  to `Logs/dcs-lso/sent/`. The settings page shows how many are waiting, with a **Send now** button (also after
  fixing a pilot token).
- After the mission, DCS's own LSO grade for each trap (from `Logs/debrief.log`, when you used carrier comms) is
  added and sent too.
- With "send to all", the other hubs also get the live calls the LSO made on that server: the uploader asks the
  server's hub for them first, so those hubs get your pass up to a minute later.
- Your trap shows up on the hub's greenie board, merged with the server's report of the same landing.
- What it's doing is logged in `Saved Games/DCS/Logs/dcs.log`, on lines starting with `DCSLSO-PILOT`.

Uploads use plain HTTP, because DCS's Lua can't do HTTPS. They contain your jet's track (and the carrier's, when visible), your DCS name and
account id (UCID), the server's address, and the sun's elevation at the time (for night passes).

## For hub admins

The hub must accept plain HTTP on `/api/v1/pilot-hook/` (no redirect to HTTPS: a redirect breaks the
upload). With Caddy:

```
http://lso.example.com {
    handle /api/v1/pilot-hook/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        redir https://{host}{uri} 308
    }
}

lso.example.com {
    reverse_proxy 127.0.0.1:8000
}
```
