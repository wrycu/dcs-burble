"""Durable upload queue: a directory of slice + sidecar pairs.

    pending/   waiting to upload (survives restarts and the hub outages)
    sent/      accepted by hub (kept as a local backup, and so a DCS grade found
               later in debrief.log can be sent for them)
    rejected/  refused by hub (4xx); kept for inspection

The sidecar JSON is written last, so a pair only counts once it is complete.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    acmi: Path
    sidecar: Path

    def meta(self) -> dict:
        return json.loads(self.sidecar.read_text(encoding="utf-8"))


class Outbox:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.sent_dir = self.root / "sent"
        self.rejected_dir = self.root / "rejected"
        for d in (self.pending_dir, self.sent_dir, self.rejected_dir):
            d.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, acmi: Path, sidecar: dict) -> Item:
        target = self.pending_dir / f"{name}.zip.acmi"
        shutil.copyfile(acmi, target)
        meta = self.pending_dir / f"{name}.json"
        tmp = meta.with_suffix(".json.part")
        tmp.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, meta)
        return Item(name, target, meta)

    def _items(self, directory: Path) -> list[Item]:
        out = []
        for meta in sorted(directory.glob("*.json")):
            name = meta.name.removesuffix(".json")
            acmi = directory / f"{name}.zip.acmi"
            if acmi.exists():
                out.append(Item(name, acmi, meta))
        return out

    def pending(self) -> list[Item]:
        return self._items(self.pending_dir)

    def sent(self) -> list[Item]:
        return self._items(self.sent_dir)

    def prune(self, directory: Path, older_than: float) -> int:
        """Remove items in `directory` (sent or rejected; never pending) last changed before `older_than`
        (a Unix time). Returns how many were removed."""
        assert directory != self.pending_dir, "pending uploads are never pruned"
        removed = 0
        for item in self._items(directory):
            if item.sidecar.stat().st_mtime < older_than:
                item.acmi.unlink(missing_ok=True)
                item.sidecar.unlink(missing_ok=True)
                removed += 1
        return removed

    def _move(self, item: Item, directory: Path) -> Item:
        acmi = directory / item.acmi.name
        meta = directory / item.sidecar.name
        os.replace(item.acmi, acmi)
        os.replace(item.sidecar, meta)
        return Item(item.name, acmi, meta)

    def requeue(self, item: Item, sidecar: dict) -> Item:
        """Send an already-sent item again with an updated sidecar (e.g. DCS grade found later)."""
        moved = self._move(item, self.pending_dir)
        moved.sidecar.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        return moved

    async def upload_pending(self, client: httpx.AsyncClient) -> tuple[int, int]:
        """Try to upload everything pending. Returns (uploaded, still pending).

        Stops at the first network/server error so the next attempt retries in order.
        """
        uploaded = 0
        items = self.pending()
        for i, item in enumerate(items):
            try:
                r = await client.post("/api/v1/passes",
                                      files={"slice": (item.acmi.name, item.acmi.read_bytes(), "application/zip")},
                                      data={"sidecar": item.sidecar.read_text(encoding="utf-8")})
            except httpx.HTTPError as exc:
                log.warning("upload of %s failed (%s); will retry", item.name, exc)
                return uploaded, len(items) - i
            if r.status_code in (200, 201):
                body = r.json()
                log.info("uploaded %s: %s (%s)", item.name, body.get("text"),
                         "new" if body.get("created") else "already on the hub")
                self._move(item, self.sent_dir)
                uploaded += 1
            elif r.status_code in (401, 403, 429) or r.status_code >= 500:
                log.warning("the hub refused %s for now (%s %s); will retry", item.name, r.status_code, r.text[:200])
                return uploaded, len(items) - i
            else:
                log.error("the hub rejected %s (%s %s); moved to rejected/", item.name, r.status_code, r.text[:200])
                self._move(item, self.rejected_dir)
        return uploaded, 0
