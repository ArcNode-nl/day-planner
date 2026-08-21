"""
Pydantic-modellen voor wat er in en uit de API gaat.

Bewust los van db.py (opslagvorm) en models.py (scheduler-dataclasses) — de
API-vorm hoeft niet 1-op-1 gelijk te lopen met hoe we dingen intern opslaan
of berekenen.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import List, Optional

from pydantic import BaseModel


class TaskCreate(BaseModel):
    id: str
    title: str
    duration_min: int
    energy: str = "medium"
    priority: int = 3
    deadline: Optional[datetime] = None
    project: Optional[str] = None
    min_block_min: int = 15
    splittable: bool = False
    not_before: Optional[time] = None  # bv. "18:00" — taak mag pas vanaf dit tijdstip
    not_after: Optional[time] = None  # bv. "09:00" — taak moet hiervoor starten


class EventCreate(BaseModel):
    title: str
    start: datetime
    end: datetime
    blocking: bool = True


class EnergyIn(BaseModel):
    level: str
    note: Optional[str] = None
    mood: Optional[int] = None  # 1 (zwaar) .. 5 (goed)


class DeferIn(BaseModel):
    note: Optional[str] = None  # bv. "geen energie voor", "verschoven naar morgen"


class AdjustDurationIn(BaseModel):
    remaining_min: int
    note: Optional[str] = None  # bv. "duurde langer dan gedacht"


class SetStartedAtIn(BaseModel):
    started_at: datetime  # handmatige (her)bevestiging/correctie van wanneer een taak echt begon


class InboxParseIn(BaseModel):
    text: str  # vrije tekst uit de snelle invoer, bv. "boodschappen doen, hoeft niet per se vandaag"


class TaskEventOut(BaseModel):
    id: int
    task_id: str
    event_type: str
    at: datetime
    energy_level: Optional[str] = None
    remaining_min_before: Optional[int] = None
    remaining_min_after: Optional[int] = None
    note: Optional[str] = None
    source: str


class PlanRequest(BaseModel):
    day_start: datetime
    day_end: datetime
    now: Optional[datetime] = None  # default: servertijd op moment van aanroep


class DayRatingIn(BaseModel):
    date: Optional[str] = None  # "YYYY-MM-DD", default: vandaag
    rating: int  # 1 (zwaar) .. 5 (goed)
    note: Optional[str] = None


class ScheduledBlockOut(BaseModel):
    start: datetime
    end: datetime
    kind: str
    task_id: Optional[str] = None
    title: str
    ai_reasoning: Optional[str] = None  # waarom de AI deze taak hier voorstelde, indien van toepassing


class PlanResponse(BaseModel):
    blocks: List[ScheduledBlockOut]
    main_goal_task_id: Optional[str] = None
    main_goal_title: Optional[str] = None
    main_goal_moneybird_minutes: Optional[int] = None  # gewerkte tijd vandaag op ditzelfde project, indien matchend
