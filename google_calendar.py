"""
Google Calendar read-only sync.

Haalt events op uit de agenda van de gebruiker en zet ze om naar EventDB-rijen.
Schrijft NOOIT terug naar Google Calendar — puur lezen. Dit is precies waarom
recurring events (vaste werkdag, wekelijkse afspraak) nu eindelijk goed werken:
die los je in Google Calendar zelf op (waar recurring events al bestaan), niet
door ze elke dag opnieuw als taak in te typen.

Voorwaarde: credentials.json (OAuth-clientgeheim, uit Google Cloud Console) en
token.json (aangemaakt via authorize_google.py, één keer handmatig) moeten in
de backend-map staan. Beide staan in .gitignore — persoonlijke geheimen, nooit
committen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, TypedDict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
BACKEND_DIR = Path(__file__).parent.parent
TOKEN_PATH = BACKEND_DIR / "token.json"


class GoogleEvent(TypedDict):
    google_event_id: str
    title: str
    start: datetime
    end: datetime


def _load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            "Geen token.json gevonden — draai eerst 'python3 authorize_google.py' "
            "vanuit de backend-map om eenmalig bij Google in te loggen."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())  # ververste token bewaren voor volgende keer
    return creds


def _parse_datetime(raw: dict) -> datetime:
    """Google's API geeft 'dateTime' (met tijdzone-offset, bv. '+02:00') voor
    tijdgebonden events. Die offset representeert al de lokale kloktijd van het
    event zelf — dus alleen de tijdzone-info eraf knippen (niet converteren,
    dat zou hier juist fout zijn, in tegenstelling tot de UTC-bug die we eerder
    bij de browser-tijden hadden)."""
    return datetime.fromisoformat(raw["dateTime"]).replace(tzinfo=None)


def fetch_events(days_ahead: int = 14) -> tuple[List[GoogleEvent], datetime, datetime]:
    """Haal aankomende events op uit de hoofdagenda, standaard komende 14 dagen.

    Hele-dag-events worden bewust overgeslagen — die tellen niet als een 'bezet'
    tijdsblok waar de scheduler taken omheen moet plannen.

    Geeft ook het gebruikte tijdvak terug (window_start, window_end, naive lokale
    tijd), zodat de caller precies weet binnen welke grenzen 'verwijderd in
    Google' gedetecteerd mag worden — buiten dat venster raken we nooit iets
    kwijt, ook al staat het er al langer niet meer in Google Calendar.
    """
    creds = _load_credentials()
    service = build("calendar", "v3", credentials=creds)

    now = datetime.utcnow()
    window_start = now
    window_end = now + timedelta(days=days_ahead)
    time_min = window_start.isoformat() + "Z"
    time_max = window_end.isoformat() + "Z"

    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,  # recurring events omzetten naar losse instanties
            orderBy="startTime",
        )
        .execute()
    )

    events: List[GoogleEvent] = []
    for item in result.get("items", []):
        if item.get("status") == "cancelled":
            continue
        start, end = item.get("start", {}), item.get("end", {})
        if "dateTime" not in start or "dateTime" not in end:
            continue  # hele-dag-event, overslaan
        events.append(
            GoogleEvent(
                google_event_id=item["id"],
                title=item.get("summary", "(geen titel)"),
                start=_parse_datetime(start),
                end=_parse_datetime(end),
            )
        )
    return events, window_start.replace(tzinfo=None), window_end.replace(tzinfo=None)
