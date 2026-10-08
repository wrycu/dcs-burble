"""Hub database (SQLAlchemy 2). SQLite for development; Postgres-compatible.

Stored ACMI slices are the source of truth; grades are derived, versioned, and can be
rebuilt at any time by regrading.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, create_engine,
                        event, inspect, text)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    """Something that uploads passes, with its own token: a DCS server's agent (kind "server": its server
    agent token), or a pilot (kind "pilot": a pilot token, tied to that pilot by `pilot_id`)."""

    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="server")  # server | pilot
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Settings the source's agent fetches (e.g. callouts: SRS server, LSO frequencies).
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # A pilot token's pilot: everything uploaded with it is credited to them, whatever name the pilot
    # flew under in DCS (that name becomes one of their aliases).
    pilot_id: Mapped[int | None] = mapped_column(ForeignKey("pilots.id"), nullable=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g. "my PC", shown to the pilot
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this server agent last reported its connected players (see PlayerSeen).
    players_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Pilot(Base):
    __tablename__ = "pilots"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # Side number (modex): the first one seen on any of the pilot's passes; later ones don't change it
    # (except by the pilot, once they've set a password).
    modex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Set by the pilot (first come, first served; an admin can reset it): then uploads without a token
    # need it to import this pilot's passes. A salted scrypt hash (hub.passwords).
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # May manage the hub on the website (/admin), signed in. Granted with `hub set-admin` or by another admin;
    # only to a pilot with a password, and lost if their password is reset.
    is_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class WebSession(Base):
    """A pilot signed in on the website (a browser): the cookie holds the token, this its hash. Ends when the
    pilot signs out, after SESSION_DAYS unused, or when their password changes or is reset."""

    __tablename__ = "web_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    pilot_id: Mapped[int] = mapped_column(ForeignKey("pilots.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PilotAlias(Base):
    """Another in-game name of a pilot (e.g. "CVW-17 | Wrycu" for Wrycu): passes reported under it are
    credited to the pilot. Seen on an upload with the pilot's token (`claimed` False), or claimed by the
    pilot with their password (`claimed` True)."""

    __tablename__ = "pilot_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    pilot_id: Mapped[int] = mapped_column(ForeignKey("pilots.id"), index=True)
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Small pieces of hub state, by key (e.g. the Discord greenie board message's id)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PlayerSeen(Base):
    """A player a server agent reported connected to its DCS server (from the server hook): lets the hub
    recognise that player's pilot hook (same UCID, from the same IP address) without a token."""

    __tablename__ = "players_seen"
    __table_args__ = (UniqueConstraint("source_id", "ucid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    ucid: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Slice(Base):
    """A stored ACMI slice, named by the SHA-256 of its bytes."""

    __tablename__ = "slices"
    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size: Mapped[int] = mapped_column(Integer)
    sidecar: Mapped[dict] = mapped_column(JSON)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pass(Base):
    __tablename__ = "passes"
    __table_args__ = (UniqueConstraint("source_id", "pass_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    # Identifies the pass independent of how it was sliced: recording start, aircraft, pass start.
    pass_key: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    slice_id: Mapped[int] = mapped_column(ForeignKey("slices.id"))
    pilot_id: Mapped[int] = mapped_column(ForeignKey("pilots.id"))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    mission: Mapped[str | None] = mapped_column(String(200))
    carrier_type: Mapped[str] = mapped_column(String(50))
    carrier_unit: Mapped[str | None] = mapped_column(String(100))
    aircraft_type: Mapped[str] = mapped_column(String(50))
    aircraft_id: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[float] = mapped_column(Float)  # mission time, seconds
    end_time: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    wire: Mapped[int | None] = mapped_column(Integer)  # from DCS's LSO only
    dcs_grade: Mapped[str | None] = mapped_column(String(200))
    # Live LSO calls the agent made during this pass: [{"time", "along", "call"}].
    calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # "pass" (None in older rows): a gradable report with the carrier. "track": one aircraft's own
    # track, no carrier (a multiplayer client's recording), graded only as part of a landing.
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The aircraft's livery, from the mission via the dcs-lso hook, when known.
    livery: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The pilot's name as reported (the in-game name), which may be an alias of `pilot`.
    reported_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # This landing's post in Discord (the per-trap messages), so it can be edited when the landing changes.
    discord_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Changed outside the running hub (e.g. `hub regrade`): its Discord post and the board are brought up to date.
    discord_stale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Flown at night (the sun below the horizon at the carrier when the pass ended); None if unknown.
    night: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Another report of the same landing (e.g. the pilot's own and the server's) is merged into the
    # landing's first gradable report, which is the one shown; see `Hub.ingest`.
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("passes.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pilot: Mapped[Pilot] = relationship()
    source: Mapped[Source] = relationship()
    slice: Mapped[Slice] = relationship()
    grades: Mapped[list[Grade]] = relationship(back_populates="pass_", order_by="Grade.id",
                                               cascade="all, delete-orphan")

    @property
    def is_track(self) -> bool:
        """No carrier in this report: an own-jet track, or one shown as a DCS-only landing (see `is_dcs_only`)."""
        return self.kind in ("track", "dcs", "rebuilt")

    @property
    def is_rebuilt(self) -> bool:
        """An own-jet track that no report with the carrier joined, graded against the carrier rebuilt from the
        jet after its trap (detect/rebuild.py). A report with the carrier still takes over if one arrives."""
        return self.kind == "rebuilt"

    @property
    def is_dcs_only(self) -> bool:
        """An own-jet track that no report with the carrier ever joined, but DCS's LSO graded: a landing graded
        by DCS alone (no trap card). A report with the carrier still takes over if one arrives."""
        return self.kind == "dcs"

    @property
    def grade(self) -> Grade | None:
        """The grade from the newest grading version."""
        return self.grades[-1] if self.grades else None


class Upload(Base):
    """A whole recording uploaded for backfill; sliced and ingested in the background."""

    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    filename: Mapped[str] = mapped_column(String(300))
    size: Mapped[int] = mapped_column(Integer)
    # inspecting -> choose_pilot (token-less, several pilots) -> needs_password (the pilot has one)
    #   -> queued -> processing -> done | failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # Uploaded without a token: only one pilot's passes are imported, picked by the uploader from those in
    # the file (`pilots`); `key` (in the uploader's link) lets only them pick.
    choose_pilot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pilots: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pilot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # One entry per pass or track found: {"kind", "pilot", "outcome", "start_time", "pass_id", "created",
    # "grade", "text", "error"}.
    results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[Source] = relationship()


class Grade(Base):
    __tablename__ = "grades"
    __table_args__ = (UniqueConstraint("pass_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pass_id: Mapped[int] = mapped_column(ForeignKey("passes.id"))
    version: Mapped[str] = mapped_column(String(20))
    grade: Mapped[str] = mapped_column(String(8))
    points: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(String(400))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pass_: Mapped[Pass] = relationship(back_populates="grades")


def make_engine(url: str) -> Engine:
    engine = create_engine(url)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _record) -> None:  # pragma: no cover - trivial
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Minimal forward migration: add nullable columns that newer versions introduced."""
    existing = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in Base.metadata.tables}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if column.name not in existing[table.name] and column.nullable:
                    ddl = column.type.compile(engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl}'))


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
