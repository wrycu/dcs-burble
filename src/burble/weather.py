"""The mission's weather as the server hook logs it (its `weather` event), read the same way by the agent's
Case III and the hub's pages."""

FOG2_MANUAL = 4  # DCS 2.9.10's fog in Manual mode (`fog2.mode`, as the mission editor saved it, 2026-11-08)


def fog(weather: dict | None) -> dict | None:
    """The mission's fog, `{"visibility_m": ..., "thickness_m": ...}`, or None: the older fog (`enable_fog`), else
    DCS 2.9.10's fog in Manual mode, at its thickest key (the lowest visibility; the keys change it over the
    mission, which isn't followed). Other `fog2` modes aren't read."""
    old = (weather or {}).get("fog")
    if old and old.get("visibility_m"):
        return old
    fog2 = (weather or {}).get("fog2") or {}
    keys = fog2.get("keys") or []
    if isinstance(keys, dict):  # Lua arrays arrive as objects keyed "1", "2", ...
        keys = [keys[k] for k in sorted(keys, key=int)]
    keys = [k for k in keys if k.get("visibility_m") and k.get("thickness_m")]
    if fog2.get("mode") != FOG2_MANUAL or not keys:
        return None
    key = min(keys, key=lambda k: k["visibility_m"])
    return {"visibility_m": key["visibility_m"], "thickness_m": key["thickness_m"]}
