"""
FastAPI-laag: dunne schil om de scheduler (scheduler.py) en de database (db.py).

Draaien vanuit backend/:
    uvicorn app.main:app --reload

Interactieve docs daarna op http://localhost:8000/docs
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from sqlmodel import Session, select

from . import ai_inbox, ai_priority, crud, google_calendar, moneybird
from .db import EnergyLogDB, EventDB, TaskDB, TaskEventDB, DayRatingDB, MoneybirdEntryDB, get_session, init_db
from .schemas import (
    AdjustDurationIn,
    DayRatingIn,
    DeferIn,
    EnergyIn,
    EventCreate,
    InboxParseIn,
    PlanRequest,
    PlanResponse,
    ScheduledBlockOut,
    SetStartedAtIn,
    TaskCreate,
    TaskEventOut,
)
from .scheduler import build_schedule, top_priority_task

app = FastAPI(title="Dagregisseur")

# Lokale Next.js dev-server draait op een andere poort (3000), dus de browser
# moet expliciet toestemming krijgen om bij deze API (8000) te komen.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    # Ook toestaan vanaf andere apparaten op je Tailscale-netwerk (100.x.x.x),
    # zodat dit ook via de laptop/telefoon werkt, niet alleen vanaf de PC zelf.
    allow_origin_regex=r"http://100\.\d{1,3}\.\d{1,3}\.\d{1,3}:3000",
    allow_methods=["*"],
    allow_headers=["*"],
)


def _as_local_naive(dt: datetime) -> datetime:
    """Normaliseer een datetime naar 'kale' lokale tijd.

    De browser stuurt tijden als UTC (JS' toISOString()), dus tijdzone-bewust
    binnen; not_before/not_after op een Task zijn kale klokttijden zonder
    tijdzone. Die twee zijn onvergelijkbaar zonder normalisatie — en simpelweg
    de tijdzone-info wegknippen zou het probleem niet oplossen, alleen stil
    verkeerd maken (UTC-cijfers aangezien voor lokale cijfers, dus in de zomer
    2 uur verschoven). We converteren daarom eerst echt naar de tijdzone van
    deze machine (die toch al lokaal bij de gebruiker draait) en pas dán
    knippen we de tijdzone-info eraf.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---- Taken ----


@app.post("/inbox/parse", response_model=ai_inbox.InboxSuggestion)
def parse_inbox_text(body: InboxParseIn):
    """Interpreteert vrije tekst als kandidaat-taak of energie-update. Puur een
    voorstel — schrijft niets weg. De frontend vult hiermee het bestaande
    taak-formulier of de energie-invoer voor, de persoon bevestigt zelf via de
    knop die er toch al staat (geen nieuw schrijfpad)."""
    suggestion = ai_inbox.parse_inbox_text(body.text)
    if suggestion is None:
        raise HTTPException(
            502, "Kon de tekst niet interpreteren — Ollama niet bereikbaar, of geen duidelijk voorstel."
        )
    return suggestion


@app.get("/tasks", response_model=List[TaskDB])
def list_tasks(session: Session = Depends(get_session)):
    return session.exec(select(TaskDB)).all()


@app.post("/tasks", response_model=TaskDB)
def create_task(task: TaskCreate, session: Session = Depends(get_session)):
    if session.get(TaskDB, task.id):
        raise HTTPException(400, f"Taak met id '{task.id}' bestaat al")
    row = TaskDB(**task.model_dump(), remaining_min=task.duration_min)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/start", response_model=TaskDB)
def start_task(task_id: str, session: Session = Depends(get_session)):
    """Markeert een taak als gestart. Los van 'done': geeft straks een
    geloofwaardig start->klaar-interval (i.p.v. alleen het eindmoment), zodat
    voorbije dagen in de weekweergave ook voor eigen taken als echte
    tijdblokjes getoond kunnen worden — net zoals nu al voor Moneybird-uren.

    Idempotent als de taak al gestart was (geen extra logregel, geen
    verandering van het oorspronkelijke starttijdstip) — bewust net als
    /plan, geen verrassingen bij een dubbele klik of pagina-herlaad."""
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    if row.done or row.cancelled:
        raise HTTPException(400, "Taak is al afgerond of geannuleerd")
    if row.started_at is not None:
        return row
    row.started_at = datetime.now()
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(
        session,
        task_id,
        "started",
        remaining_min_before=row.remaining_min,
        remaining_min_after=row.remaining_min,
    )
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/pause", response_model=TaskDB)
def pause_task(task_id: str, session: Session = Depends(get_session)):
    """Zet een gestarte taak tijdelijk 'on hold' — started_at blijft staan
    (dat blijft het eerste-keer-gestart-moment), maar de taak telt vanaf nu
    niet meer als actief. Voor de scheduler betekent dit: niet langer
    verankerd op die starttijd, mag weer vrij ingepland worden — anders zou
    een 'vergeten' lopende taak voor altijd zijn tijdvak blijven blokkeren."""
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    if row.started_at is None:
        raise HTTPException(400, "Taak is nog niet gestart, kan niet gepauzeerd worden")
    if row.done or row.cancelled:
        raise HTTPException(400, "Taak is al afgerond of geannuleerd")
    if row.paused:
        return row
    row.paused = True
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(session, task_id, "paused")
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/resume", response_model=TaskDB)
def resume_task(task_id: str, session: Session = Depends(get_session)):
    """Hervat een gepauzeerde taak. started_at blijft ongewijzigd (het
    oorspronkelijke moment blijft zichtbaar in bv. 'bezig sinds'), alleen de
    paused-vlag gaat eraf — de taak is dan weer verankerd zoals voorheen."""
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    if not row.paused:
        return row
    row.paused = False
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(session, task_id, "resumed")
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/set-started-at", response_model=TaskDB)
def set_started_at(task_id: str, body: SetStartedAtIn, session: Session = Depends(get_session)):
    """Zet started_at handmatig, los van de live /start-knop.

    Bedoeld voor de weekweergave: een voorbije taak waarvoor de start niet
    gemeten is (en dus een gok toonde — vorige-taak-klaar of een tijdvak-hint)
    kan hiermee alsnog een echte, door de gebruiker bevestigde starttijd
    krijgen. Werkt bewust ook op al afgeronde taken — een correctie op het
    verleden is per definitie een correctie op iets dat al 'done' is. Eenmaal
    gezet is er geen onderscheid meer met een taak die je live startte: beide
    zijn dan gewoon de vastgestelde waarheid, geen aparte 'correctie'-status
    die je apart moet blijven meeslepen.
    """
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    row.started_at = _as_local_naive(body.started_at)
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(
        session,
        task_id,
        "started_at_corrected",
        note=f"handmatig gezet op {body.started_at.isoformat()}",
    )
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/done", response_model=TaskDB)
def mark_task_done(task_id: str, session: Session = Depends(get_session)):
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    before = row.remaining_min
    row.done = True
    row.remaining_min = 0
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(session, task_id, "done", remaining_min_before=before, remaining_min_after=0)
    session.refresh(row)  # log_task_event's eigen commit() expired 'row' net, opnieuw laden
    return row


@app.post("/tasks/{task_id}/cancel", response_model=TaskDB)
def cancel_task(task_id: str, body: DeferIn, session: Session = Depends(get_session)):
    """Taak wordt niet gedaan en komt niet meer terug in de backlog.

    Anders dan 'done': dit is een taak die je bewust laat vallen, geen taak die
    je hebt afgerond. Beide zijn losse event_types in de log, want ze betekenen
    iets anders voor latere patroonherkenning.
    """
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    before = row.remaining_min
    row.cancelled = True
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(
        session, task_id, "cancelled", remaining_min_before=before, remaining_min_after=before, note=body.note
    )
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/defer", response_model=TaskDB)
def defer_task(task_id: str, body: DeferIn, session: Session = Depends(get_session)):
    """Taak blijft gewoon open in de backlog (voor een volgende /plan-aanroep),
    maar we loggen dat 'ie vandaag niet is gebeurd. Dit is het directe
    feedbacksignaal: 'geen energie voor' of 'wilde het kwijt maar het paste niet'.
    """
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    if row.done or row.cancelled:
        raise HTTPException(400, "Taak is al afgerond of geannuleerd")
    crud.log_task_event(
        session,
        task_id,
        "deferred",
        remaining_min_before=row.remaining_min,
        remaining_min_after=row.remaining_min,
        note=body.note,
    )
    session.refresh(row)
    return row


@app.post("/tasks/{task_id}/adjust-duration", response_model=TaskDB)
def adjust_task_duration(
    task_id: str, body: AdjustDurationIn, session: Session = Depends(get_session)
):
    """Bijstellen hoeveel tijd een taak nog nodig heeft — bv. bleek langer/korter
    te duren dan geschat. Wordt gelogd zodat de leerlaag later kan zien hoe
    goed jouw eigen tijdsinschattingen kloppen."""
    row = session.get(TaskDB, task_id)
    if not row:
        raise HTTPException(404, "Taak niet gevonden")
    before = row.remaining_min
    row.remaining_min = body.remaining_min
    session.add(row)
    session.commit()
    session.refresh(row)
    crud.log_task_event(
        session,
        task_id,
        "duration_changed",
        remaining_min_before=before,
        remaining_min_after=body.remaining_min,
        note=body.note,
    )
    session.refresh(row)
    return row


@app.get("/tasks/events", response_model=List[TaskEventOut])
def list_task_events(session: Session = Depends(get_session)):
    """De volledige plan_vs_actual-log, ruw. Later de bron voor patroonherkenning
    en voor een eventuele Moneybird-vergelijking."""
    return session.exec(select(TaskEventDB).order_by(TaskEventDB.at.desc())).all()


# ---- Calendar-events (tijdelijk handmatig, tot Google Calendar-sync er is) ----


@app.get("/events", response_model=List[EventDB])
def list_events(session: Session = Depends(get_session)):
    return session.exec(select(EventDB)).all()


@app.post("/events", response_model=EventDB)
def create_event(event: EventCreate, session: Session = Depends(get_session)):
    row = EventDB(**event.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@app.post("/calendar/sync")
def sync_calendar(session: Session = Depends(get_session)):
    """Haalt events op uit Google Calendar (komende 14 dagen) en zet ze om
    naar EventDB-rijen. Read-only — schrijft nooit terug naar Google. Events
    die in Google zelf verwijderd zijn, worden ook lokaal opgeruimd (alleen
    binnen het opgehaalde tijdvak)."""
    try:
        events, window_start, window_end = google_calendar.fetch_events(days_ahead=14)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Kon Google Calendar niet bereiken: {e}")

    created, updated = crud.upsert_google_events(session, events)
    valid_ids = {ev["google_event_id"] for ev in events}
    deleted = crud.delete_stale_google_events(session, valid_ids, window_start, window_end)
    return {"created": created, "updated": updated, "deleted": deleted, "total_fetched": len(events)}


@app.post("/moneybird/sync")
def sync_moneybird(session: Session = Depends(get_session)):
    """Haalt gewerkte uren op uit Moneybird (afgelopen 30 dagen). Read-only —
    schrijft nooit terug naar Moneybird."""
    try:
        entries = moneybird.fetch_time_entries(days_back=30)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Kon Moneybird niet bereiken: {e}")

    created, updated = crud.upsert_moneybird_entries(session, entries)
    return {"created": created, "updated": updated, "total_fetched": len(entries)}


@app.get("/moneybird/entries", response_model=List[MoneybirdEntryDB])
def list_moneybird_entries(session: Session = Depends(get_session)):
    return session.exec(select(MoneybirdEntryDB).order_by(MoneybirdEntryDB.started_at)).all()


# ---- Energie ----


@app.post("/energy", response_model=EnergyLogDB)
def set_energy(energy: EnergyIn, session: Session = Depends(get_session)):
    if energy.mood is not None and not (1 <= energy.mood <= 5):
        raise HTTPException(400, "mood moet tussen 1 en 5 liggen")
    row = EnergyLogDB(level=energy.level, as_of=datetime.now(), note=energy.note, mood=energy.mood)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@app.get("/energy/current", response_model=EnergyLogDB)
def get_current_energy(session: Session = Depends(get_session)):
    row = session.exec(select(EnergyLogDB).order_by(EnergyLogDB.as_of.desc())).first()
    if not row:
        raise HTTPException(404, "Nog geen energie opgegeven")
    return row


# ---- Dagbeoordeling (reflectie achteraf, los van de momentopnames in EnergyLogDB) ----


@app.post("/day-rating", response_model=DayRatingDB)
def set_day_rating(body: DayRatingIn, session: Session = Depends(get_session)):
    if not (1 <= body.rating <= 5):
        raise HTTPException(400, "rating moet tussen 1 en 5 liggen")
    date_str = body.date or datetime.now().strftime("%Y-%m-%d")

    row = session.exec(select(DayRatingDB).where(DayRatingDB.date == date_str)).first()
    if row:
        row.rating = body.rating
        row.note = body.note
        row.at = datetime.now()
    else:
        row = DayRatingDB(date=date_str, rating=body.rating, note=body.note)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@app.get("/day-ratings", response_model=List[DayRatingDB])
def list_day_ratings(session: Session = Depends(get_session)):
    return session.exec(select(DayRatingDB).order_by(DayRatingDB.date)).all()


# ---- Plannen ----


@app.post("/plan", response_model=PlanResponse)
def plan_day(req: PlanRequest, session: Session = Depends(get_session)):
    """Bouw (of herbouw) de dagplanning vanaf 'now'.

    Dit endpoint is zowel 'Maak mijn dag' als 'Herplan vanaf nu' — beide zijn
    dezelfde aanroep, want de scheduler rekent altijd vanaf 'now' met de
    actuele remaining_min/done-status uit de database.
    """
    now = _as_local_naive(req.now) if req.now else datetime.now()
    day_start = _as_local_naive(req.day_start)
    day_end = _as_local_naive(req.day_end)

    tasks = crud.load_open_tasks(session)
    events = crud.load_events(session, day_start, day_end)
    energy = crud.load_current_energy(session, now)

    # AI-voorstel voor de volgorde van vandaag, op basis van energie + de volledige
    # plan_vs_actual-log (nog klein genoeg om compleet mee te geven — zodra dat niet
    # meer past, wordt dit een aparte samenvat/ophaal-stap, geen gewoon "meer tekst").
    # Puur in-memory, nooit teruggeschreven naar de DB — /plan blijft een voorstel.
    all_log = session.exec(select(TaskEventDB).order_by(TaskEventDB.at)).all()
    suggestions = ai_priority.suggest_priorities(tasks, energy, all_log)

    reasoning_by_task: dict[str, str] = {}
    if suggestions:
        for t in tasks:
            s = suggestions.get(t.id)
            if s:
                t.priority = s.priority  # alleen voor déze planning, niet persistent
                t.preferred_not_before = s.preferred_not_before
                t.preferred_not_after = s.preferred_not_after
                reasoning_by_task[t.id] = s.reasoning

    # Hoofddoel bepalen VOORDAT build_schedule de taken muteert (remaining_min
    # loopt anders al terug en de score klopt dan niet meer).
    main_goal = top_priority_task(tasks, energy, now)

    # Als het hoofddoel een project heeft, kijken hoeveel Moneybird-uren daar
    # deze week al op geboekt zijn. Bewust een WEEK-venster, niet "vandaag":
    # Moneybird-uren zijn altijd achteraf gelogd (vaak pas 's avonds of in een
    # batch), dus "vandaag" zou 's ochtends vrijwel altijd 0 tonen — precies
    # wanneer je 'm het meest zou willen zien.
    main_goal_moneybird_minutes = None
    if main_goal and main_goal.project:
        week_start = (day_start - timedelta(days=day_start.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        main_goal_moneybird_minutes = crud.moneybird_minutes_matching(
            session, main_goal.project, week_start, day_start.replace(hour=23, minute=59)
        )

    # LET OP: build_schedule muteert task.remaining_min in-memory om de dag te
    # kunnen vullen, maar dat wordt hier bewust NIET teruggeschreven naar de DB.
    # /plan is een voorstel, geen commitment — alleen /tasks/{id}/done (of later
    # een 'log voortgang'-endpoint) mag remaining_min/done echt wijzigen.
    schedule = build_schedule(day_start, day_end, now, events, tasks, energy)

    return PlanResponse(
        blocks=[
            ScheduledBlockOut(
                start=b.start,
                end=b.end,
                kind=b.kind,
                task_id=b.task_id,
                title=b.title,
                ai_reasoning=reasoning_by_task.get(b.task_id) if b.task_id else None,
            )
            for b in schedule
        ],
        main_goal_task_id=main_goal.id if main_goal else None,
        main_goal_title=main_goal.title if main_goal else None,
        main_goal_moneybird_minutes=main_goal_moneybird_minutes,
    )
