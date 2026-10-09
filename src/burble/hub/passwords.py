"""Pilot passwords: salted scrypt hashes (Python's standard library), and a simple limit on wrong guesses."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

MIN_LENGTH = 8
_N, _R, _P = 2 ** 14, 8, 1  # scrypt cost (about 16 MB and a few tens of ms per check)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                   dklen=len(digest) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


class FailureLimiter:
    """At most `limit` wrong passwords per key (e.g. a pilot) in `window_s`; in memory, per process."""

    def __init__(self, limit: int = 10, window_s: float = 3600.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < self.window_s]
            self._failures[key] = recent
            return len(recent) >= self.limit

    def failed(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic() if now is None else now)
