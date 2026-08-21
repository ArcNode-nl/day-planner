"""
Vertaalt tussen DB-rijen (db.py) en de pure dataclasses waar de scheduler
(scheduler.py) mee rekent (models.py). Geen scheduling-logica hier.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from .db import EnergyLogDB, EventDB, MoneybirdEntryDB, TaskDB, TaskEventDB
from .models import CalendarEvent, Energy, EnergyState, Task


def load_open_tasks(session: Session) -> List[Task]:
    rows = session.exec(
        select(TaskDB).where(TaskDB.done == False, TaskDB.cancelled == False)  # noqa: E712
    ).all()
    return [
        Task(
            id=r.id,
            title=r.title,
            duration_min=r.duration_min,
            energy=Energy(r.energy),
            priority=r.priority,
            deadline=r.deadline,
            project=r.project,
            min_block_min=r.min_block_min,
            splittable=r.splittable,
            not_before=r.not_before,
            not_after=r.not_after,
            done=r.done,
            started_at=r.started_at,
            paused=r.paused,
            remaining_min=r.remaining_min,
        )
        for r in rows
    ]


def latest_energy_level(session: Session) -> Optional[str]:
    row = session.exec(select(EnergyLogDB).order_by(EnergyLogDB.as_of.desc())).first()
    return row.level if row else None


def log_task_event(
    session: Session,
    task_id: str,
    event_type: str,
    remaining_min_before: Optional[int] = None,
    remaining_min_after: Optional[int] = None,
    note: Optional[str] = None,
) -> TaskEventDB:
    """Schrijf één regel naar de plan_vs_actual-log. Append-only, nooit wijzigen."""
    row = TaskEventDB(
        task_id=task_id,
        event_type=event_type,
        at=datetime.now(),
        energy_level=latest_energy_level(session),
        remaining_min_before=remaining_min_before,
        remaining_min_after=remaining_min_after,
        note=note,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def save_task_progress(session: Session, tasks: List[Task]) -> None:
    """Schrijf remaining_min/done terug naar de DB na een geplande dag.

    Dit is waarom /plan zowel 'Maak mijn dag' als 'Herplan vanaf nu' kan zijn:
    elke aanroep leest verse state uit de DB, rekent, en schrijft meteen terug.
    Geen gedeelde Python-objecten die tussen requests blijven hangen.
    """
    for t in tasks:
        row = session.get(TaskDB, t.id)
        if row:
            row.remaining_min = t.remaining_min
            row.done = t.remaining_min <= 0
            session.add(row)
    session.commit()


def load_events(session: Session, day_start: datetime, day_end: datetime) -> List[CalendarEvent]:
    rows = session.exec(
        select(EventDB).where(EventDB.end > day_start, EventDB.start < day_end)
    ).all()
    return [
        CalendarEvent(title=r.title, start=r.start, end=r.end, blocking=r.blocking)
        for r in rows
    ]


def upsert_google_events(session: Session, events: list) -> tuple[int, int]:
    """Zet opgehaalde Google-events om naar EventDB-rijen. Bestaande rijen
    (zelfde google_event_id) worden bijgewerkt i.p.v. gedupliceerd — zo kan
    /calendar/sync gewoon herhaaldelijk aangeroepen worden. Geeft
    (aantal_nieuw, aantal_bijgewerkt) terug."""
    created = updated = 0
    for ev in events:
        row = session.exec(
            select(EventDB).where(EventDB.google_event_id == ev["google_event_id"])
        ).first()
        if row:
            row.title = ev["title"]
            row.start = ev["start"]
            row.end = ev["end"]
            session.add(row)
            updated += 1
        else:
            session.add(
                EventDB(
                    title=ev["title"],
                    start=ev["start"],
                    end=ev["end"],
                    google_event_id=ev["google_event_id"],
                )
            )
            created += 1
    session.commit()
    return created, updated


def delete_stale_google_events(
    session: Session, valid_ids: set, window_start: datetime, window_end: datetime
) -> int:
    """Verwijder gesynchte events die niet meer in de nieuwste ophaal-set zitten
    — dus in Google Calendar zelf verwijderd zijn. Bewust alleen binnen het
    zojuist opgehaalde tijdvak: zo raken we nooit events kwijt die buiten dit
    venster liggen, ook al staan ze al langer niet meer in Google."""
    rows = session.exec(
        select(EventDB).where(
            EventDB.google_event_id.is_not(None),
            EventDB.start >= window_start,
            EventDB.start < window_end,
        )
    ).all()
    deleted = 0
    for row in rows:
        if row.google_event_id not in valid_ids:
            session.delete(row)
            deleted += 1
    session.commit()
    return deleted


def upsert_moneybird_entries(session: Session, entries: list) -> tuple[int, int]:
    """Zelfde upsert-patroon als Google Calendar: bestaande rijen (zelfde
    moneybird_id) bijwerken, nieuwe aanmaken. Geeft (nieuw, bijgewerkt) terug."""
    created = updated = 0
    for e in entries:
        row = session.exec(
            select(MoneybirdEntryDB).where(MoneybirdEntryDB.moneybird_id == e["moneybird_id"])
        ).first()
        if row:
            row.started_at = e["started_at"]
            row.ended_at = e["ended_at"]
            row.description = e["description"]
            row.project = e["project"]
            session.add(row)
            updated += 1
        else:
            session.add(MoneybirdEntryDB(**e))
            created += 1
    session.commit()
    return created, updated


def moneybird_minutes_matching(
    session: Session, project_hint: str, day_start: datetime, day_end: datetime
) -> int:
    """Som van gewerkte minuten in Moneybird-entries die op deze dag vielen en
    waarvan project of omschrijving de project-hint bevat (hoofdletter-
    ongevoelig). Bewust een exacte, deterministische tekstmatch in code — geen
    AI — precies zoals de task_id-matching eerder vandaag."""
    rows = session.exec(
        select(MoneybirdEntryDB).where(
            MoneybirdEntryDB.started_at >= day_start,
            MoneybirdEntryDB.started_at < day_end,
        )
    ).all()
    hint = project_hint.lower()
    total = 0
    for r in rows:
        haystack = f"{r.project or ''} {r.description}".lower()
        if hint in haystack:
            total += int((r.ended_at - r.started_at).total_seconds() // 60)
    return total


def load_current_energy(session: Session, now: datetime) -> EnergyState:
    row = session.exec(select(EnergyLogDB).order_by(EnergyLogDB.as_of.desc())).first()
    if not row:
        return EnergyState(
            level=Energy.MEDIUM, as_of=now, note="geen energie opgegeven, default medium"
        )
    return EnergyState(level=Energy(row.level), as_of=row.as_of, note=row.note)
