"""Content-addressed storage for ACMI slices (local disk; S3-compatible later)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class SliceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}.zip.acmi"

    def put(self, data: bytes) -> str:
        """Store bytes (idempotent); returns their SHA-256."""
        sha = hashlib.sha256(data).hexdigest()
        target = self.path(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash never leaves a partial file under the final name.
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".part")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        return sha
