"""
Eenmalig te draaien om bij Google in te loggen en een token.json aan te maken:

    cd backend
    python3 authorize_google.py

Opent een browser waarin je bij Google inlogt en alleen-lezen-toestemming
geeft voor je agenda. Daarna staat er een token.json in deze map — die hoeft
nooit meer opnieuw aangemaakt te worden (ververst zichzelf automatisch),
tenzij je 'm verwijdert of de toegang bij Google intrekt.

Vereist credentials.json in dezelfde map. Zie het bijbehorende
GOOGLE_CALENDAR_SETUP.md voor hoe je die uit Google Cloud Console haalt.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH = Path(__file__).parent / "token.json"

if __name__ == "__main__":
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(
            f"Geen credentials.json gevonden op {CREDENTIALS_PATH}.\n"
            "Zie GOOGLE_CALENDAR_SETUP.md voor hoe je die uit Google Cloud Console haalt."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    print(f"Gelukt! Token opgeslagen in {TOKEN_PATH}")
