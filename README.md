# Dagregisseur — v0.1

Lokale persoonlijke dagplanner. Geen chatbot, geen cloud-kosten voor de kernlogica.

## Wat er nu staat (getest, werkt lokaal)

```
backend/
  requirements.txt
  app/
    models.py       # Task, CalendarEvent, EnergyState, ScheduledBlock
    scheduler.py     # build_schedule() + replan_from_now() — puur Python, geen LLM
    dummy_data.py    # voorbeeldagenda + taken om mee te testen
    test_run.py      # draai dit om de scheduler te zien werken
```

## Zelf draaien

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m app.test_run
```

Dit print een dagplanning op basis van nepdata, én laat zien hoe
"herplannen vanaf nu" werkt als een taak uitloopt en een andere klaar is.

## Hoe de scheduler denkt

- Calendar-events zijn hard: die worden er als blokken uitgesneden, taken
  vullen de gaten ertussen.
- Elke taak krijgt een score op basis van prioriteit, deadline-druk en of
  de benodigde energie past bij je huidige energie (`_score()` in
  `scheduler.py`) — zwaardere taken dan je aankunt worden afgestraft.
- Splitsbare taken (`splittable=True`) mogen over meerdere gaten verdeeld
  worden; niet-splitsbare taken wachten op een gat dat groot genoeg is.
- `build_schedule()` is stateless: geef 'm de huidige tijd, agenda, taken
  en energie, en hij herberekent gewoon alles vanaf dat moment. Dat is
  meteen de "Herplan vanaf nu"-knop — er is geen aparte replan-logica nodig.

## Volgende stappen (niet gebouwd, expres)

1. **FastAPI eromheen** — dunne laag: `POST /plan` (roept `build_schedule`
   aan), `GET /tasks`, `POST /tasks`, `POST /tasks/{id}/done`.
2. **SQLite/SQLModel** — taken en energie-log persistent maken i.p.v. dummy_data.
3. **Google Calendar OAuth (read-only)** — events ophalen i.p.v. hardcoded lijst.
4. **Ollama voor NL-inbox** — vrije tekst → kandidaat-Task, met een strikt
   JSON-schema in de prompt; jij accepteert/corrigeert voordat het een
   echte taak wordt.
5. **Next.js UI** — dagtijdlijn (calendar + task-blokken), inbox, energie-slider,
   "Maak mijn dag" / "Herplan vanaf nu"-knoppen.

Elke stap hierboven is los te bouwen en te testen zonder de vorige stappen
kapot te maken — dat was ook het idee.
