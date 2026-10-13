"""The receiver's database (`telemetry.db`, its own SQLite file; none of the hub's tables): every report received,
each install's days (one row per day, replaced when a newer report covers it), API keys and the password.
And the stats worked out from them (`usage`), for the Usage page and `/api/v1/usage`."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from ..hub.passwords import MIN_LENGTH, hash_password, verify_password

MAX_REPORT_BYTES = 64 * 1024
REPORT_EVERY = timedelta(hours=1)  # at most one report per install this often
DAY_FIELDS = ("landings", "pilots", "server_only", "pilot_only", "both", "live_calls")
MAX_COUNT = 10_000_000
WEEKS = 26  # landings per week on the Usage page


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    install: Mapped[str] = mapped_column(String(36), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    version: Mapped[str] = mapped_column(String(40))
    report: Mapped[dict] = mapped_column(JSON)  # as sent, apart from `days` (kept in `days`)


class Day(Base):
    __tablename__ = "days"
    __table_args__ = (UniqueConstraint("install", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    install: Mapped[str] = mapped_column(String(36), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    counts: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


PASSWORD_SETTING = "password"


class ReportError(ValueError):
    pass


class TooSoon(Exception):
    pass


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _count(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNT:
        raise ReportError(f"{what} isn't a count")
    return value


def _versions(value, what: str) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 50:
        raise ReportError(f"{what} isn't a list of versions")
    return {str(k)[:40]: _count(n, what) for k, n in value.items()}


def check_report(body: dict) -> dict:
    """The report, checked against its shape; anything else in it is dropped. Raises ReportError."""
    if not isinstance(body, dict) or body.get("schema") != 1:
        raise ReportError("not a usage report (schema 1)")
    try:
        install = str(uuid.UUID(str(body.get("install"))))
    except ValueError:
        raise ReportError("install isn't an id") from None
    version = body.get("version")
    if not isinstance(version, str) or not 0 < len(version) <= 40:
        raise ReportError("version missing")
    days = body.get("days")
    if not isinstance(days, list) or not 0 < len(days) <= 31:
        raise ReportError("days missing")
    clean_days = []
    for d in days:
        if not isinstance(d, dict) or not isinstance(d.get("day"), str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", d["day"]):
            raise ReportError("a day without its date")
        date.fromisoformat(d["day"])
        clean_days.append({"day": d["day"], **{f: _count(d.get(f, 0), f) for f in DAY_FIELDS}})
    agents, hooks = body.get("agents") or {}, body.get("pilot_hooks") or {}
    if not isinstance(agents, dict) or not isinstance(hooks, dict):
        raise ReportError("agents or pilot hooks aren't counts")
    return {"schema": 1, "install": install, "version": version, "days": clean_days,
            "pilots_30d": _count(body.get("pilots_30d", 0), "pilots_30d"),
            "agents": {"live_calls": _count(agents.get("live_calls", 0), "agents"),
                       "collect_only": _count(agents.get("collect_only", 0), "agents"),
                       "versions": _versions(agents.get("versions", {}), "agent versions")},
            "pilot_hooks": {"count": _count(hooks.get("count", 0), "pilot hooks"),
                            "versions": _versions(hooks.get("versions", {}), "pilot hook versions")}}


class Store:
    def __init__(self, data_dir: str | Path) -> None:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{(path / 'telemetry.db').resolve()}")

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _record) -> None:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        Base.metadata.create_all(self.engine)
        self.sessions: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)

    # -- reports ------------------------------------------------------------------------------

    def receive(self, body: dict, now: datetime | None = None) -> None:
        """Store a report (checked with `check_report`). Raises ReportError, or TooSoon within an hour of the
        install's previous report."""
        report = check_report(body)
        now = now or utcnow()
        with self.sessions.begin() as s:
            last = s.scalar(select(Report.received_at).where(Report.install == report["install"])
                            .order_by(Report.received_at.desc()).limit(1))
            if last is not None and now - _aware(last) < REPORT_EVERY:
                raise TooSoon()
            kept = {k: v for k, v in report.items() if k != "days"}
            s.add(Report(install=report["install"], received_at=now, version=report["version"], report=kept))
            existing = {d.day: d for d in s.scalars(select(Day).where(
                Day.install == report["install"], Day.day.in_([d["day"] for d in report["days"]])))}
            for d in report["days"]:
                counts = {f: d[f] for f in DAY_FIELDS}
                if d["day"] in existing:
                    existing[d["day"]].counts, existing[d["day"]].updated_at = counts, now
                else:
                    s.add(Day(install=report["install"], day=d["day"], counts=counts, updated_at=now))

    # -- access -------------------------------------------------------------------------------

    def set_password(self, password: str) -> None:
        if len(password) < MIN_LENGTH:
            raise ValueError(f"the password needs at least {MIN_LENGTH} characters")
        with self.sessions.begin() as s:
            row = s.get(Setting, PASSWORD_SETTING)
            if row is None:
                s.add(Setting(key=PASSWORD_SETTING, value=hash_password(password)))
            else:
                row.value = hash_password(password)

    def check_password(self, password: str) -> bool:
        with self.sessions() as s:
            row = s.get(Setting, PASSWORD_SETTING)
            return verify_password(password, row.value if row else None)

    def new_key(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("name the key (e.g. the hub it's for)")
        key = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            if s.scalar(select(ApiKey).where(ApiKey.name == name)) is not None:
                raise ValueError(f"there's already a key named {name!r}")
            s.add(ApiKey(name=name, key_hash=_hash_key(key)))
        return key

    def revoke_key(self, name: str) -> None:
        with self.sessions.begin() as s:
            row = s.scalar(select(ApiKey).where(ApiKey.name == name))
            if row is None:
                raise LookupError(f"no key named {name!r}")
            s.delete(row)

    def keys(self) -> list[ApiKey]:
        with self.sessions() as s:
            return list(s.scalars(select(ApiKey).order_by(ApiKey.name)))

    def allowed(self, secret: str) -> bool:
        """An API key, or the password."""
        if not secret:
            return False
        with self.sessions() as s:
            if s.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_key(secret))) is not None:
                return True
        return self.check_password(secret)

    # -- stats --------------------------------------------------------------------------------

    def usage(self, now: datetime | None = None) -> dict:
        """What the Usage page shows: installs, and per install and in total landings, pilots, the track
        breakdown, live calls vs collection only, versions; landings per week."""
        now = now or utcnow()
        with self.sessions() as s:
            reports = list(s.scalars(select(Report).order_by(Report.received_at)))
            days = list(s.scalars(select(Day)))
        first: dict[str, datetime] = {}
        latest: dict[str, Report] = {}
        for r in reports:
            first.setdefault(r.install, _aware(r.received_at))
            latest[r.install] = r
        cutoff_30 = (now - timedelta(days=30)).date().isoformat()
        sums: dict[str, Counter] = defaultdict(Counter)
        recent: dict[str, Counter] = defaultdict(Counter)
        weekly: Counter = Counter()
        for d in days:
            sums[d.install].update(d.counts)
            if d.day > cutoff_30:
                recent[d.install].update(d.counts)
            day = date.fromisoformat(d.day)
            weekly[(day - timedelta(days=day.weekday())).isoformat()] += d.counts.get("landings", 0)

        def seen_within(install: str, n: int) -> bool:
            return now - _aware(latest[install].received_at) <= timedelta(days=n)

        installs = []
        for install, r in sorted(latest.items(), key=lambda kv: kv[1].received_at, reverse=True):
            rep = r.report
            installs.append({
                "install": install, "first_seen": first[install].isoformat(),
                "last_seen": _aware(r.received_at).isoformat(), "version": r.version,
                "landings": sums[install]["landings"], "landings_30d": recent[install]["landings"],
                "pilots_30d": rep.get("pilots_30d", 0),
                "tracks": {k: sums[install][k] for k in ("server_only", "pilot_only", "both")},
                "live_call_landings": sums[install]["live_calls"],
                "agents": rep.get("agents", {}), "pilot_hooks": rep.get("pilot_hooks", {})})
        active = [i for i in installs if seen_within(i["install"], 30)]

        def merged(key: str) -> dict[str, int]:
            c: Counter = Counter()
            for i in active:
                c.update(i[key].get("versions", {}))
            return dict(c)

        this_week = now.date() - timedelta(days=now.date().weekday())
        weeks = [(this_week - timedelta(weeks=n)).isoformat() for n in range(WEEKS - 1, -1, -1)]
        return {
            "generated_at": now.isoformat(),
            "installs": {"ever": len(installs), "last_7d": sum(1 for i in installs if seen_within(i["install"], 7)),
                         "last_30d": len(active)},
            "totals": {"landings": sum(i["landings"] for i in installs),
                       "landings_30d": sum(i["landings_30d"] for i in installs),
                       "pilots_30d": sum(i["pilots_30d"] for i in active),
                       "tracks": {k: sum(i["tracks"][k] for i in installs) for k in ("server_only", "pilot_only", "both")},
                       "live_call_landings": sum(i["live_call_landings"] for i in installs),
                       "agents": {k: sum(i["agents"].get(k, 0) for i in active) for k in ("live_calls", "collect_only")},
                       "pilot_hooks": sum(i["pilot_hooks"].get("count", 0) for i in active)},
            "versions": {"hub": dict(Counter(i["version"] for i in active)), "agent": merged("agents"),
                         "pilot_hook": merged("pilot_hooks")},
            "weekly": [{"week": w, "landings": weekly.get(w, 0)} for w in weeks],
            "per_install": installs,
        }
