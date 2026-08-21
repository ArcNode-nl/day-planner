"""
AI-laag: vraagt Ollama om vrije tekst ("boodschappen doen, hoeft niet per se
vandaag") te interpreteren als kandidaat-taak of energie-update.

Zelfde ontwerpprincipe als ai_priority.py: dit is een voorstel, geen schrijf-
actie. De persoon bevestigt via de bestaande /tasks- of /energy-flow — er is
hier bewust geen nieuw schrijfpad, alleen een nieuwe manier om het bestaande
formulier voor te vullen.

Anders dan ai_priority.py heeft dit GEEN geschiedenis nodig om te werken: het
is een stateless parse van precies één stukje tekst, dus de eerdere
matching-problematiek (7B-model dat afhaakt bij veel taken tegelijk) speelt
hier niet — er is maar één ding om over te redeneren per aanroep.

Vangnet: als Ollama niet draait, een timeout geeft, of onzin teruggeeft, geeft
deze functie gewoon None terug. De aanroeper (main.py) laat de persoon dan
weten dat het voorstel niet lukte en gewoon zelf het formulier in te vullen —
geen halve of verzonnen data die per ongeluk als voorstel oogt.

DAGDEEL-BESEF (16 augustus, ontdekt via testen): "vanavond boodschappen doen"
kreeg aanvankelijk GEEN tijdvak, want de prompt vertaalde alleen letterlijke
kloktijden ("na 18:00") naar not_before/not_after — een dagdeel-woord als
"vanavond" viel daar niet onder en werd genegeerd. Dit is dezelfde
"geen dagdeel-besef"-beperking die al in ROADMAP.md stond voor de scheduler
zelf, nu ook zichtbaar in de NL-inbox. Opgelost door dagdeel-woorden
(vanochtend/vanmiddag/vanavond/vannacht) expliciet naar een grove
not_before/not_after te laten vertalen in de prompt, i.p.v. alleen exacte
kloktijden.
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Optional

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b-instruct"
TIMEOUT_SECONDS = 60.0  # lokale 7B-modellen kunnen 'koud' traag zijn (model laden in RAM)


class InboxSuggestion(BaseModel):
    kind: str  # "task" | "energy" | "unclear"
    reasoning: str

    # Alleen ingevuld als kind == "task" — zelfde velden/betekenis als NewTask.
    task_title: Optional[str] = None
    task_duration_min: Optional[int] = None
    task_energy: Optional[str] = None  # "low" | "medium" | "high"
    task_priority: Optional[int] = None  # 1 (hoog) .. 5 (laag)
    task_splittable: Optional[bool] = None
    task_not_before: Optional[time] = None
    task_not_after: Optional[time] = None

    # Alleen ingevuld als kind == "energy" — zelfde velden/betekenis als EnergyIn.
    energy_level: Optional[str] = None  # "low" | "medium" | "high"
    energy_note: Optional[str] = None


def _build_prompt(text: str) -> str:
    return f"""Je bent onderdeel van een persoonlijke dagplanner. De gebruiker tikt vrije
tekst in een snelle invoer. Dat kan een NIEUWE TAAK zijn, een ENERGIE-UPDATE
(hoe iemand zich nu voelt), of soms geen van beide/onduidelijk.

Bepaal eerst welk type dit is (`kind`): "task", "energy", of "unclear".

Als het een TAAK is, vul de task_*-velden:
  - task_title: korte titel, ZONDER tijdsaanduidingen erin (die horen in de
    tijdvak-velden, niet in de titel zelf)
  - task_duration_min: schat een redelijke duur in minuten als die niet
    expliciet genoemd wordt (bv. "boodschappen doen" ≈ 30, "stofzuigen" ≈ 20,
    "mailtje sturen" ≈ 10). Noem nooit 0 of null.
  - task_energy: "low" | "medium" | "high" — hoe zwaar de TAAK ZELF aanvoelt
    om te doen, dit is iets anders dan hoe de gebruiker zich nu voelt
  - task_priority: 1 (hoog) .. 5 (laag), default 3 als niets in de tekst op
    hoog of laag belang wijst
  - task_splittable: true als de taak logisch in stukken gedaan kan worden
    (bv. "rapport schrijven"), anders false
  - task_not_before / task_not_after: invullen bij een EXPLICIETE kloktijd
    ("na 18:00", "voor 10 uur") ÉN bij een DAGDEEL-aanduiding, ook als er geen
    exacte kloktijd genoemd wordt — dit is een bekend, veelvoorkomend geval,
    behandel het net zo serieus als een letterlijke tijd:
      • "vanochtend" / "'s ochtends" → task_not_before="06:00", task_not_after="12:00"
      • "vanmiddag" / "'s middags"   → task_not_before="12:00", task_not_after="18:00"
      • "vanavond" / "'s avonds"     → task_not_before="18:00" (task_not_after leeg)
      • "vannacht" / "'s nachts"     → task_not_before="22:00" (task_not_after leeg)
    "pas na X" → task_not_before=X (mag niet eerder beginnen); "voor X" /
    "moet om X" → task_not_after=X (moet uiterlijk dan gebeurd zijn). Laat
    allebei op null als er ECHT geen tijdsaanduiding is (letterlijk noch
    dagdeel) — verzin er dan niets bij.

Als het een ENERGIE-UPDATE is, vul:
  - energy_level: "low" | "medium" | "high"
  - energy_note: een korte samenvatting van de reden, dicht bij de eigen
    woorden van de gebruiker

Bij "unclear": laat alle task_*/energy_*-velden op null, alleen reasoning
invullen (kort, waarom dit niet duidelijk als taak of energie te lezen is).

Geef ALLEEN geldige JSON terug, exact dit schema, niets ervoor of erna:
{{"kind": "task", "reasoning": "...", "task_title": null,
"task_duration_min": null, "task_energy": null, "task_priority": null,
"task_splittable": null, "task_not_before": null, "task_not_after": null,
"energy_level": null, "energy_note": null}}

Tekst van de gebruiker:
"{text}\""""


def parse_inbox_text(text: str) -> Optional[InboxSuggestion]:
    """Interpreteer één stukje vrije tekst. Geeft None terug bij een lege
    input of als de AI-aanroep om wat voor reden dan ook mislukt."""
    if not text.strip():
        return None

    prompt = _build_prompt(text.strip())
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
        suggestion = InboxSuggestion.model_validate_json(content)
    except (httpx.HTTPError, KeyError, ValidationError, ValueError) as e:
        logger.warning(
            "NL-inbox-voorstel mislukt (%s: %s) — geen voorstel, persoon vult zelf in.",
            type(e).__name__,
            e,
        )
        return None

    if suggestion.kind not in ("task", "energy", "unclear"):
        return None
    return suggestion
