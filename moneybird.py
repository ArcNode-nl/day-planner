"""
Moneybird read-only sync — historische gewerkte uren binnenhalen.

Puur lezen, net als Google Calendar: nooit iets terugschrijven naar Moneybird.
Gebruikt een persoonlijk API-token (geen OAuth-flow nodig voor persoonlijk
gebruik van één administratie) — aan te maken op
https://moneybird.com/user/applications/new, scope 'time_entries' aanvinken.

Voorwaarde: moneybird_token.txt in de backend-map, met alleen het token erin.
Staat in .gitignore — persoonlijk geheim, nooit committen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, TypedDict

import httpx

BACKEND_DIR = Path(__file__).parent.parent
TOKEN_PATH = BACKEND_DIR / "moneybird_token.txt"
BASE_URL = "https://moneybird.com/api/v2"


class MoneybirdEntry(TypedDict):
    moneybird_id: str
    started_at: datetime
    ended_at: datetime
    description: str
    project: Optional[str]


def _as_local_naive(dt: datetime) -> datetime:
    """Zelfde conversie als main.py's _as_local_naive — Moneybird geeft
    started_at/ended_at terug met tijdzone-info (UTC of een offset). Die info
    zomaar wegknippen (zoals dit bestand eerder deed) zet de UTC-cijfers stil
    om voor lokale cijfers, met een verschil van 1-2 uur als gevolg — vandaar
    eerst echt converteren naar de tijdzone van deze machine, en pas dán de
    tijdzone-info eraf knippen."""
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _parse_moneybird_datetime(raw: str) -> datetime:
    # Python < 3.11 kent fromisoformat() geen 'Z'-suffix (UTC-notatie) — dat
    # normaliseren we zelf, robuuster dan aannemen dat de draaiende Python-
    # versie dit al ondersteunt.
    return _as_local_naive(datetime.fromisoformat(raw.replace("Z", "+00:00")))


def _load_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            "Geen moneybird_token.txt gevonden — maak een persoonlijk token aan op "
            "https://moneybird.com/user/applications/new (scope 'time_entries') en zet "
            "'m in backend/moneybird_token.txt."
        )
    return TOKEN_PATH.read_text().strip()


def _administration_id(token: str) -> str:
    """Haal de eerste (enige, voor persoonlijk gebruik) administratie op die bij
    dit token hoort — scheelt dat de gebruiker zelf een ID hoeft op te zoeken."""
    resp = httpx.get(
        f"{BASE_URL}/administrations.json",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    resp.raise_for_status()
    admins = resp.json()
    if not admins:
        raise RuntimeError("Geen administraties gevonden bij dit Moneybird-token.")
    return str(admins[0]["id"])


def fetch_time_entries(days_back: int = 30) -> List[MoneybirdEntry]:
    """Haal gewerkte uren op van de afgelopen 'days_back' dagen."""
    token = _load_token()
    admin_id = _administration_id(token)

    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")

    resp = httpx.get(
        f"{BASE_URL}/{admin_id}/time_entries.json",
        headers={"Authorization": f"Bearer {token}"},
        params={"filter": f"period:{start}..{end}"},
        timeout=30.0,
    )
    resp.raise_for_status()

    entries: List[MoneybirdEntry] = []
    for item in resp.json():
        started = item.get("started_at")
        ended = item.get("ended_at")
        if not started or not ended:
            continue  # lopende/onvolledige entry, overslaan
        project = item.get("project")
        entries.append(
            MoneybirdEntry(
                moneybird_id=str(item["id"]),
                started_at=_parse_moneybird_datetime(started),
                ended_at=_parse_moneybird_datetime(ended),
                description=item.get("description") or "(geen omschrijving)",
                project=project.get("name") if project else None,
            )
        )
    return entries
