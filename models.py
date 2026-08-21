"""
Datamodel voor de dagregisseur.

Dit bestand bevat GEEN logica, alleen de vorm van de data waar de scheduler
(scheduler.py) mee rekent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Optional


class Energy(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Ruwe volgorde van zwaarte, gebruikt om taak-energie te vergelijken met
# beschikbare energie van de gebruiker.
ENERGY_RANK = {Energy.LOW: 0, Energy.MEDIUM: 1, Energy.HIGH: 2}


@dataclass
class CalendarEvent:
    """Een harde, niet-verplaatsbare afspraak (uit Google Calendar)."""

    title: str
    start: datetime
    end: datetime
    blocking: bool = True  # False = 'vrij' event, telt niet als bezet

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Task:
    """Een taak uit de takenvoorraad (backlog)."""

    id: str
    title: str
    duration_min: int  # geschatte totale duur
    energy: Energy = Energy.MEDIUM  # benodigde energie om dit te doen
    priority: int = 3  # 1 (hoog) .. 5 (laag)
    deadline: Optional[datetime] = None
    project: Optional[str] = None
    min_block_min: int = 15  # kleinste zinvolle brok als taak gesplitst wordt
    splittable: bool = False
    not_before: Optional[time] = None  # taak mag pas vanaf dit tijdstip op de dag
    not_after: Optional[time] = None  # taak moet vóór dit tijdstip afgerond/gestart zijn
    preferred_not_before: Optional[time] = None  # ZACHTE AI-hint, geen harde blokkade
    preferred_not_after: Optional[time] = None  # idem
    done: bool = False
    started_at: Optional[datetime] = None  # wanneer de taak echt gestart is, indien gestart
    paused: bool = False  # tijdelijk 'on hold', started_at blijft staan maar telt niet als actief
    remaining_min: int = field(default=-1)  # -1 = "nog niet begonnen"

    def __post_init__(self) -> None:
        if self.remaining_min < 0:
            self.remaining_min = self.duration_min


@dataclass
class EnergyState:
    """Hoe belastbaar de gebruiker zich voelt, met tijdstempel."""

    level: Energy
    as_of: datetime
    note: Optional[str] = None  # bv. "vannacht laat doorgegaan"


@dataclass
class ScheduledBlock:
    """Eén blok in de uiteindelijke dagplanning."""

    start: datetime
    end: datetime
    kind: str  # "calendar" | "task" | "buffer" | "free"
    task_id: Optional[str] = None
    title: str = ""

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)
