"""
Database-laag: SQLite via SQLModel.

Losse tabellen voor taken, calendar-events (tijdelijk handmatig, tot Google
Calendar-sync er is) en een energie-log. Dit bestand bevat GEEN scheduling-
logica — dat blijft in scheduler.py, dat puur blijft werken op de dataclasses
uit models.py.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

DATABASE_URL = "sqlite:///dagregisseur.db"
engine = create_engine(DATABASE_URL, echo=False)


class TaskDB(SQLModel, table=True):
    id: str = Field(primary_key=True)
    title: str
    duration_min: int
    energy: str = "medium"  # "low" | "medium" | "high"
    priority: int = 3
    deadline: Optional[datetime] = None
    project: Optional[str] = None
    min_block_min: int = 15
    splittable: bool = False
    not_before: Optional[time] = None
    not_after: Optional[time] = None
    done: bool = False
    cancelled: bool = False
    remaining_min: int
    started_at: Optional[datetime] = None
    # Los van started_at: een gestarte taak die je even laat liggen zonder 'm
    # af te ronden. started_at blijft staan (dat blijft het eerste-keer-
    # gestart-moment), maar de scheduler mag 'm dan weer vrij inplannen —
    # anders blokkeert een 'vergeten' lopende taak voor altijd zijn tijdvak.
    paused: bool = False


class EventDB(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    start: datetime
    end: datetime
    blocking: bool = True
    google_event_id: Optional[str] = Field(default=None, index=True, unique=True)


class EnergyLogDB(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    level: str  # "low" | "medium" | "high"
    as_of: datetime
    note: Optional[str] = None
    mood: Optional[int] = None  # 1 (zwaar) .. 5 (goed) — los van energie


class TaskEventDB(SQLModel, table=True):
    """De plan_vs_actual-log: elke afwijking tussen plan en werkelijkheid.

    event_type: "started" | "done" | "cancelled" | "deferred" | "duration_changed"
    Dit is bewust een append-only log (nooit updaten/verwijderen) — dat is
    precies de dataset waar de latere leerlaag patronen uit moet halen.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str
    event_type: str
    at: datetime
    energy_level: Optional[str] = None  # energie op het moment van de gebeurtenis
    remaining_min_before: Optional[int] = None
    remaining_min_after: Optional[int] = None
    note: Optional[str] = None
    source: str = "app"  # "app" | "moneybird" (voor later)


class DayRatingDB(SQLModel, table=True):
    """Eén subjectieve terugblik per dag: hoe ging de dag als geheel (1-5).

    Los van EnergyLogDB (dat zijn momentopnames gedurende de dag) — dit is een
    reflectie áchteraf, meestal 's avonds. Eén rij per datum; opnieuw invullen
    overschrijft de vorige rating van diezelfde dag."""

    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True, unique=True)  # "YYYY-MM-DD", makkelijker te matchen dan een datetime
    rating: int  # 1 (zwaar) .. 5 (goed), zelfde schaal als mood
    note: Optional[str] = None
    at: datetime = Field(default_factory=datetime.now)


class MoneybirdEntryDB(SQLModel, table=True):
    """Historische gewerkte uren, gesynchroniseerd vanuit Moneybird. Read-only
    kopie — we schrijven hier nooit iets naartoe, alleen upserts vanuit sync."""

    id: Optional[int] = Field(default=None, primary_key=True)
    moneybird_id: str = Field(index=True, unique=True)
    started_at: datetime
    ended_at: datetime
    description: str
    project: Optional[str] = None


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
