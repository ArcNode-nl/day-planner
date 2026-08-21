"""
AI-laag: vraagt een lokaal taalmodel (Ollama) om een voorstel voor de volgorde
van vandaag's taken, op basis van de plan_vs_actual-log.

Dit is bewust de ENIGE plek waar een taalmodel de dagplanning beïnvloedt — en
zelfs hier beslist het niet zelf: het doet een voorstel (prioriteit + een kort
"waarom" + evt. een zachte tijdvoorkeur) dat de deterministische scheduler
(scheduler.py) verder verwerkt. Kloktijden, botsingen en energie-scoring
blijven daar, puur Python.

BELANGRIJK ONTWERPPUNT (ontdekt via testen): het matchen van log-entries aan
taken via task_id hoort NIET bij het model thuis, ook al lijkt dat triviaal.
Met een klein setje (2 taken, 2 log-regels) deed een lokaal 7B-model dat prima,
maar zodra het er 7-op-7 werden, viel het terug op een generiek "geen
geschiedenis" voor alles — een bekend patroon: bij oplopende complexiteit
grijpt een klein model naar het veiligste antwoord in plaats van 7x
daadwerkelijk te correleren. Matching is precies het soort exacte, mechanische
werk waar gewone code betrouwbaar in is en een taalmodel niet per se. Daarom
groeperen we de log hier zelf per taak (in Python, exacte ID-match) vóórdat er
iets naar Ollama gaat: elke taak in de prompt heeft zijn eigen geschiedenis al
rechtstreeks erbij staan, het model hoeft nooit te zoeken. Taken zonder
geschiedenis gaan niet eens naar de AI — daar is toch niets aan toe te voegen.

Vangnet: als Ollama niet draait, een timeout geeft, of onzin teruggeeft,
gebeurt er hier NIETS geks — de taken-zonder-geschiedenis krijgen alsnog hun
deterministische "geen geschiedenis"-antwoord, en de rest valt terug op de
bestaande statische prioriteit. Een AI-storing mag nooit de dag breken.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import time
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel, ValidationError

from .db import TaskEventDB
from .models import EnergyState, Task

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"
TIMEOUT_SECONDS = 60.0  # lokale 7B-modellen kunnen 'koud' traag zijn (model laden in RAM)

NO_HISTORY_REASONING = "Geen eerdere geschiedenis voor deze taak."


class PrioritySuggestion(BaseModel):
    task_id: str
    priority: int  # 1 (hoog) .. 5 (laag) — zelfde schaal als het bestaande veld
    reasoning: str
    preferred_not_before: Optional[time] = None  # ZACHTE hint, bv. "18:00" — geen blokkade
    preferred_not_after: Optional[time] = None


class PrioritySuggestionList(BaseModel):
    suggestions: List[PrioritySuggestion]


def _group_by_task(log: List[TaskEventDB]) -> Dict[str, List[TaskEventDB]]:
    grouped: Dict[str, List[TaskEventDB]] = defaultdict(list)
    for e in log:
        grouped[e.task_id].append(e)
    return grouped


def _build_prompt(tasks: List[Task], log_by_task: Dict[str, List[TaskEventDB]]) -> str:
    """Bouw de prompt voor taken die ALLEMAAL al gematchte geschiedenis hebben —
    de matching is al gebeurd vóór deze functie wordt aangeroepen."""
    blocks = []
    for t in tasks:
        entries = log_by_task.get(t.id, [])
        history = "\n".join(
            f'  - {e.at:%Y-%m-%d %H:%M} [{e.event_type}] note="{e.note or ""}"' for e in entries
        )
        blocks.append(
            f'- id={t.id} titel="{t.title}" duur={t.duration_min}min '
            f"huidige_prioriteit={t.priority} project={t.project or '-'}\n"
            f"  Eerdere aanpassingen voor DEZE taak:\n{history}"
        )
    task_blocks = "\n".join(blocks)

    return f"""Je bent onderdeel van een persoonlijke dagplanner. Hieronder staan taken
mét hun eigen geschiedenis van eerdere aanpassingen (uitstel, annulering,
duur-correctie) er al direct bij. Je enige taak: kijk of die geschiedenis iets
zegt over hoe je VANDAAG deze taak zou moeten inplannen, en stel op basis
daarvan een prioriteit voor (1=hoog..5=laag) plus een kort zinnetje waarom.

BELANGRIJK — wat je NIET moet doen: energie-matching, deadline-urgentie en
tijdsplanning gebeuren al elders in het systeem met gewone code. Ga daar niet
over redeneren of iets over verzinnen — dat voegt niets toe en is dubbel werk.
Jouw enige toegevoegde waarde is de vrije-tekst geschiedenis interpreteren.

Als een note een KLOKTIJD-voorkeur uitdrukt, zet die om naar het juiste veld —
let hier goed op, dit gaat vaak fout:
  • "dit doe ik pas na 18:00" / "niet voor 18:00" → preferred_not_before="18:00"
    (de taak mag niet EERDER dan dit tijdstip beginnen)
  • "moet voor 9 uur" / "liever voor 10 uur" → preferred_not_after="09:00"
    (de taak moet UITERLIJK op dit tijdstip gebeuren, niet later)
Twijfel je? "pas na X" is bijna altijd not_before=X, "voor X" is bijna altijd
not_after=X. NIET vertalen naar een hogere prioriteit — dat is een ander veld
met een andere betekenis. Laat een tijdveld op null als die note er niet is.
Dit zijn zachte hints, geen harde blokkades — de planner respecteert ze waar
mogelijk, maar wijkt ervan af als er anders niets te plannen valt.

Niet elke note hoeft relevant te zijn voor prioriteit of tijd — bv. een
duur-correctie ("duurde langer dan gedacht") zegt iets over de geschatte duur,
niet over wanneer/hoe belangrijk. Verzin daar niets bovenop; laat prioriteit
en tijdvelden dan gewoon zoals ze waren.

Drie concrete voorbeelden van fouten die vaak misgaan — let hier extra op:
  1. Note = ALLEEN een tijdstip, geen reden qua belangrijkheid (bv. "pas na
     18:00", "liever voor 10 uur"): vul het tijdveld in, maar laat priority
     exact op huidige_prioriteit staan. Een tijdvoorkeur is geen
     belangrijkheids-oordeel — verlaag de prioriteit NIET erbij.
  2. Note = ALLEEN een duur-correctie (bv. "duurde langer dan gedacht"): laat
     priority ÉN beide tijdvelden ongewijzigd. Dit zegt niets over prioriteit
     of timing.
  3. Als je zelf in je reasoning een tijdstip noemt (bv. "moet voor 10:00"),
     zorg dan dat het bijbehorende tijdveld ook daadwerkelijk is ingevuld —
     niet op null laten staan terwijl je reasoning wél een tijd noemt.

Taken (elk met zijn eigen geschiedenis):
{task_blocks}

Geef ALLEEN geldige JSON terug, exact dit schema, niets ervoor of erna:
{{"suggestions": [{{"task_id": "...", "priority": 1, "reasoning": "...",
"preferred_not_before": null, "preferred_not_after": null}}]}}
Eén entry per bovenstaande taak-id, geen extra's, geen weglatingen."""


def suggest_priorities(
    tasks: List[Task], energy: EnergyState, log: List[TaskEventDB]
) -> Optional[Dict[str, PrioritySuggestion]]:
    """Geeft per taak een voorstel terug. Taken zonder geschiedenis krijgen
    deterministisch (geen AI-aanroep) een 'geen geschiedenis'-antwoord; alleen
    taken mét geschiedenis gaan naar Ollama. Bij falen van de AI-aanroep blijft
    het deterministische deel gewoon overeind — geen alles-of-niets meer."""
    if not tasks:
        return None

    log_by_task = _group_by_task(log)
    tasks_with_history = [t for t in tasks if log_by_task.get(t.id)]
    tasks_without_history = [t for t in tasks if not log_by_task.get(t.id)]

    result: Dict[str, PrioritySuggestion] = {
        t.id: PrioritySuggestion(task_id=t.id, priority=t.priority, reasoning=NO_HISTORY_REASONING)
        for t in tasks_without_history
    }

    if not tasks_with_history:
        return result or None

    prompt = _build_prompt(tasks_with_history, log_by_task)
    try:
        resp = httpx.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",  # dwingt Ollama tot geldige JSON-output
                "keep_alive": "10m",  # model warm houden, anders herlaadt elk verzoek opnieuw
            },
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        parsed = PrioritySuggestionList.model_validate_json(content)
    except (httpx.HTTPError, KeyError, ValidationError, ValueError) as e:
        # Ollama niet bereikbaar, timeout, ongeldige JSON, onverwacht schema —
        # het deterministische deel (result) blijft gewoon staan; alleen het
        # AI-deel voor tasks_with_history ontbreekt dan. Wél loggen, anders is
        # dit onmogelijk te diagnosticeren als het misgaat.
        logger.warning(
            "AI-prioriteitsvoorstel mislukt (%s: %s) — val voor deze taken terug op statische prioriteit.",
            type(e).__name__,
            e,
        )
        return result

    valid_ids = {t.id for t in tasks_with_history}
    for s in parsed.suggestions:
        if s.task_id in valid_ids and 1 <= s.priority <= 5:
            result[s.task_id] = s

    return result
