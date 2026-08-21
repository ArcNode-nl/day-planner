"""
Scheduling engine.

Puur deterministisch: geen LLM hier. Input is calendar (hard), taken (zacht,
te herschikken) en een energie-toestand. Output is een lijst ScheduledBlock's
die samen de (resterende) dag vullen.

Kernidee:
1. Bepaal vrije gaten tussen calendar-events (binnen day_start/day_end, vanaf 'now').
2. Score elke openstaande taak t.o.v. de huidige energie, deadline-druk en prioriteit.
3. Vul de gaten grofweg gulzig (greedy) met de hoogst scorende taak die op dit
   moment zowel qua tijdvak (not_before/not_after) als qua grootte past, splits
   taken die 'splittable' zijn als een gat te klein is voor de volle duur.
4. Zet een korte buffer tussen taken van verschillende projecten/zwaarte,
   zodat er geen onrealistisch strak schema ontstaat.

Tijdvakken (not_before/not_after) zijn generiek: of de reden nou "ik eet nooit
voor 18:00" is of "buiten-taak, dus alleen als het straks droog is" — beide
zijn gewoon een venster op de dag, de scheduler maakt daar geen onderscheid in.

Taken met een started_at (via de Start-knop of achteraf gecorrigeerd) zijn
AL BEZIG en dus niet meer herplanbaar: die krijgen een vast blok op hun eigen
starttijd, net als een calendar-event, i.p.v. mee te dingen om een gat vanaf
'now'. Zonder dit zou "Herplan vanaf nu" een lopende taak elke keer weer naar
het huidige moment schuiven, wat het hele idee van 'gestart' ondermijnt.

replan_from_now() is de "Herplan vanaf nu"-knop: geeft dezelfde output,
maar rekent alleen met wat er ná 'now' nog moet gebeuren.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from .models import (
    CalendarEvent,
    Energy,
    ENERGY_RANK,
    EnergyState,
    ScheduledBlock,
    Task,
)

BUFFER_MIN = 10  # standaard pauze tussen taken


def _free_slots(
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    events: List[CalendarEvent],
) -> List[tuple[datetime, datetime]]:
    """Bereken vrije tijdvakken tussen nu en day_end, calendar-events eruit gesneden."""
    start = max(day_start, now)
    blocking = sorted(
        [e for e in events if e.blocking and e.end > start and e.start < day_end],
        key=lambda e: e.start,
    )

    slots: List[tuple[datetime, datetime]] = []
    cursor = start
    for ev in blocking:
        ev_start = max(ev.start, start)
        if ev_start > cursor:
            slots.append((cursor, ev_start))
        cursor = max(cursor, ev.end)
    if cursor < day_end:
        slots.append((cursor, day_end))

    # negeer verwaarloosbare snippertjes
    return [(s, e) for s, e in slots if (e - s) >= timedelta(minutes=10)]


def _energy_penalty(task_energy: Energy, current: Energy) -> float:
    """Hoe slechter een taak past bij de huidige energie, hoe hoger de penalty."""
    diff = ENERGY_RANK[task_energy] - ENERGY_RANK[current]
    if diff <= 0:
        return 0.0  # taak is lichter dan of gelijk aan wat je aankunt: prima
    return diff * 6.0  # taak is zwaarder dan je energie: fors afstraffen


def _deadline_urgency(task: Task, now: datetime) -> float:
    if not task.deadline:
        return 0.0
    hours_left = max((task.deadline - now).total_seconds() / 3600, 0.1)
    return 10.0 / hours_left  # hoe dichterbij de deadline, hoe hoger


def _hard_window_urgency(task: Task, now: datetime) -> float:
    """Zelfde idee als _deadline_urgency, maar dan voor een harde not_after.
    Zonder dit weegt een taak met een verstrijkende deadline niet zwaarder dan
    een gelijk-geprioriteerde taak zonder deadline — met als risico dat die
    laatste de laatste ruimte vóór de deadline opsoupeert en de deadline-taak
    daardoor helemaal uit de planning valt."""
    if not task.not_after:
        return 0.0
    deadline_dt = datetime.combine(now.date(), task.not_after)
    if deadline_dt <= now:
        return 0.0  # deadline al voorbij, kan toch niet meer — geen kunstmatige boost
    hours_left = max((deadline_dt - now).total_seconds() / 3600, 0.1)
    return 10.0 / hours_left


def _score(task: Task, current_energy: Energy, now: datetime) -> float:
    score = 0.0
    score += (6 - task.priority) * 2.0  # priority 1 (hoog) weegt zwaar
    score += _deadline_urgency(task, now)
    score += _hard_window_urgency(task, now)
    score -= _energy_penalty(task.energy, current_energy)
    return score


def _window_ok(task: Task, at: datetime) -> bool:
    """Mag deze taak op dit moment van de dag beginnen? (HARDE grens)"""
    t = at.time()
    if task.not_before and t < task.not_before:
        return False
    if task.not_after and t >= task.not_after:
        return False
    return True


def _soft_window_ok(task: Task, at: datetime) -> bool:
    """Past dit moment bij de ZACHTE AI-voorkeur? Geen blokkade — alleen gebruikt
    om bij voorkeur een ander gat te zoeken vóórdat deze taak alsnog hier landt."""
    t = at.time()
    if task.preferred_not_before and t < task.preferred_not_before:
        return False
    if task.preferred_not_after and t >= task.preferred_not_after:
        return False
    return True


def _max_minutes_by_window(task: Task, at: datetime) -> Optional[int]:
    """Hoeveel minuten mag dit blok nog duren voordat not_after ingaat?
    None = geen beperking."""
    if not task.not_after:
        return None
    window_end = datetime.combine(at.date(), task.not_after)
    if window_end <= at:
        return 0
    return int((window_end - at).total_seconds() // 60)


def _next_window_open(remaining_tasks: List[Task], at: datetime, before: datetime) -> Optional[datetime]:
    """Vroegste moment waarop een nu-nog-niet-beschikbare taak alsnog mag beginnen
    — hard óf zacht venster — voor zover dat nog binnen dit slot valt. None als
    niemand nog opengaat. Dit is bewust de eerste optie vóór we een zachte
    voorkeur laten varen: liever twee uur wachten op het juiste venster dan een
    taak meteen op het verkeerde moment proppen."""
    candidates = []
    for t in remaining_tasks:
        for bound in (t.not_before, t.preferred_not_before):
            if bound and bound > at.time():
                candidates.append(datetime.combine(at.date(), bound))
    candidates = [c for c in candidates if c < before]
    return min(candidates) if candidates else None


def _find_placeable(
    remaining_tasks: List[Task], cursor: datetime, slot_left: int, require_soft: bool
):
    """Zoek de hoogst scorende taak die hier en nu past. Met require_soft=True
    wordt ook de zachte AI-voorkeur gerespecteerd; met False alleen de harde
    grens — dat is de fallback als er anders niets te plaatsen valt."""
    for idx, task in enumerate(remaining_tasks):
        if not _window_ok(task, cursor):
            continue
        if require_soft and not _soft_window_ok(task, cursor):
            continue

        fit_min = min(task.remaining_min, slot_left)
        window_cap = _max_minutes_by_window(task, cursor)
        if window_cap is not None:
            fit_min = min(fit_min, window_cap)

        too_small = fit_min < task.min_block_min and fit_min < task.remaining_min
        if too_small:
            continue

        return idx, task, fit_min, window_cap
    return None


def _place_task(
    blocks: List[ScheduledBlock],
    remaining_tasks: List[Task],
    cursor: datetime,
    slot_end: datetime,
    found,
) -> datetime:
    """Zet één taakblok neer, werk remaining_tasks bij, voeg evt. een buffer
    toe. Geeft de nieuwe cursor-positie terug."""
    idx, task, fit_min, window_cap = found

    block_end = cursor + timedelta(minutes=fit_min)
    blocks.append(
        ScheduledBlock(start=cursor, end=block_end, kind="task", task_id=task.id, title=task.title)
    )
    task.remaining_min -= fit_min
    cursor = block_end

    if task.remaining_min <= 0:
        remaining_tasks.pop(idx)
    elif window_cap is not None and fit_min == window_cap:
        # taak liep tegen not_after aan: voor vandaag klaar, wat er nog over
        # is past niet meer in het venster van vandaag.
        remaining_tasks.pop(idx)

    if cursor + timedelta(minutes=BUFFER_MIN) < slot_end and remaining_tasks:
        blocks.append(
            ScheduledBlock(
                start=cursor, end=cursor + timedelta(minutes=BUFFER_MIN), kind="buffer", title="pauze"
            )
        )
        cursor += timedelta(minutes=BUFFER_MIN)

    return cursor


def top_priority_task(tasks: List[Task], energy: EnergyState, now: datetime) -> Optional[Task]:
    """De taak met de hoogste score op dit moment — het 'hoofddoel' van de dag.
    Puur informatief (voor de UI), beïnvloedt de eigenlijke planning niet.

    Een al gestarte, niet-gepauzeerde taak wint hier altijd: je bent er al mee
    bezig, dus die is feitelijk al het hoofddoel, ongeacht wat er verder nog
    scoort. Een gepauzeerde taak telt hier niet mee — daar ben je momenteel
    juist niet mee bezig."""
    open_tasks = [t for t in tasks if not t.done and t.remaining_min > 0]
    if not open_tasks:
        return None
    started = [t for t in open_tasks if t.started_at is not None and not t.paused]
    if started:
        return min(started, key=lambda t: t.started_at)  # het langst lopende eerst
    return max(open_tasks, key=lambda t: _score(t, energy.level, now))


def build_schedule(
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    events: List[CalendarEvent],
    tasks: List[Task],
    energy: EnergyState,
) -> List[ScheduledBlock]:
    """Bouw een volledige dagplanning vanaf 'now'."""

    all_open = [t for t in tasks if not t.done and t.remaining_min > 0]

    # Al gestarte taken zijn niet meer herplanbaar: die krijgen een vast blok
    # op hun eigen starttijd (net als een calendar-event) i.p.v. mee te dingen
    # om een gat vanaf 'now'. Zonder dit zou elke "Herplan vanaf nu" een
    # lopende taak weer naar het huidige moment schuiven.
    anchored_tasks = [t for t in all_open if t.started_at is not None and not t.paused]
    pending_tasks = [t for t in all_open if t.started_at is None or t.paused]

    open_tasks = sorted(
        pending_tasks,
        key=lambda t: _score(t, energy.level, now),
        reverse=True,
    )

    # Pseudo-calendar-events voor de gaten-berekening, zodat pending taken niet
    # over een al lopende taak heen gepland worden.
    anchored_events = [
        CalendarEvent(
            title=t.title,
            start=t.started_at,
            end=t.started_at + timedelta(minutes=max(t.remaining_min, 1)),
            blocking=True,
        )
        for t in anchored_tasks
    ]

    slots = _free_slots(day_start, day_end, now, events + anchored_events)
    blocks: List[ScheduledBlock] = []

    # Calendar-events zelf ook als blokken toevoegen, voor een compleet dagbeeld.
    for ev in events:
        if ev.blocking and ev.end > now and ev.start < day_end:
            blocks.append(
                ScheduledBlock(
                    start=max(ev.start, now),
                    end=ev.end,
                    kind="calendar",
                    title=ev.title,
                )
            )

    # Gestarte taken als vast blok, op hun ECHTE starttijd (niet afgekapt op
    # 'now' zoals bij calendar-events hierboven) — de hele reden hiervoor is
    # juist dat 08:22 zichtbaar 08:22 blijft, ook als 'now' inmiddels 09:40 is.
    for t in anchored_tasks:
        blocks.append(
            ScheduledBlock(
                start=t.started_at,
                end=t.started_at + timedelta(minutes=max(t.remaining_min, 1)),
                kind="task",
                task_id=t.id,
                title=t.title,
            )
        )

    remaining_tasks = list(open_tasks)

    for slot_start, slot_end in slots:
        cursor = slot_start
        while remaining_tasks:
            slot_left = int((slot_end - cursor).total_seconds() // 60)
            if slot_left < 10:
                break

            # Volgorde van voorkeur, elke stap alleen geprobeerd als de vorige
            # niets opleverde:
            #  1. Iets plaatsen dat zowel de harde grens als de zachte
            #     AI-voorkeur respecteert.
            #  2. Nog niks? Kijk of er verderop in dit slot een venster opengaat
            #     (hard óf zacht) — dan liever wachten (vrije ruimte invoegen en
            #     doorspringen) dan een voorkeur meteen negeren.
            #  3. Ook dat niet? Dan pas de zachte voorkeur laten varen en
            #     plaatsen wat wél binnen de harde grens past — beter een
            #     voorkeur schenden dan de rest van de dag leeg laten.
            found = _find_placeable(remaining_tasks, cursor, slot_left, require_soft=True)
            if found is not None:
                cursor = _place_task(blocks, remaining_tasks, cursor, slot_end, found)
                continue

            jump_to = _next_window_open(remaining_tasks, cursor, slot_end)
            if jump_to:
                blocks.append(
                    ScheduledBlock(start=cursor, end=jump_to, kind="free", title="vrije ruimte")
                )
                cursor = jump_to
                continue

            found = _find_placeable(remaining_tasks, cursor, slot_left, require_soft=False)
            if found is not None:
                cursor = _place_task(blocks, remaining_tasks, cursor, slot_end, found)
                continue

            break  # echt niets meer te doen in dit slot

        if cursor < slot_end:
            blocks.append(
                ScheduledBlock(start=cursor, end=slot_end, kind="free", title="vrije ruimte")
            )

    return sorted(blocks, key=lambda b: b.start)


def replan_from_now(
    day_start: datetime,
    day_end: datetime,
    now: datetime,
    events: List[CalendarEvent],
    tasks: List[Task],
    energy: EnergyState,
) -> List[ScheduledBlock]:
    """Alias die de intentie expliciet maakt: dit is de 'Herplan vanaf nu'-knop.

    Verleden blijft verleden (blocks vóór 'now' worden hier niet meer opnieuw
    berekend, de caller behoudt die uit de vorige planning); alleen het
    resterende deel van de dag wordt herverdeeld over openstaande taken.
    """
    return build_schedule(day_start, day_end, now, events, tasks, energy)
