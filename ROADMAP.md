# Dagregisseur — roadmap & visie

Laatste update: 17 augustus 2026

## Wat er al staat (werkend, lokaal getest)

- **Scheduler** (`backend/app/scheduler.py`) — puur Python, geen LLM. Vult calendar-gaten
  met taken op basis van score (prioriteit, deadline-urgentie, energie-match).
  Een gestarte, niet-gepauzeerde taak is verankerd op zijn echte starttijd
  (niet herplanbaar); zodra je 'm pauzeert valt-ie terug in de normale
  gaten-vulling, net als elke andere open taak.
- **Start / Pauzeer / Hervat** — `POST /tasks/{id}/start`, `/pause`, `/resume`.
  `started_at` is het eerste-keer-gestart-moment en blijft ook tijdens een
  pauze staan; `paused` is een los vlaggetje dat alleen bepaalt of de taak
  momenteel actief (verankerd) is. Achteraf corrigeren kan via
  `/tasks/{id}/set-started-at` (bv. vanuit de weekweergave).
- **NL-inbox** (`backend/app/ai_inbox.py`, `POST /inbox/parse`) — vrije tekst
  → voorstel voor een nieuwe taak (titel/duur/energie/prioriteit/tijdvak) of
  een energie-update. Puur een voorstel, vult het bestaande formulier voor;
  bevestigen gaat via de bestaande knoppen ("Toevoegen aan backlog" of "Log
  nu" — dat laatste maakt de taak aan én start 'm meteen, voor als je nu al
  bezig bent i.p.v. iets voor later plant). Herkent ook dagdeel-woorden
  (vanochtend/vanmiddag/vanavond/vannacht) als impliciet tijdvak, niet alleen
  letterlijke kloktijden.
- **API** (FastAPI + SQLite) — taken, calendar-events, energie, `/plan` (dubbelt als
  "Maak mijn dag" en "Herplan vanaf nu"; is bewust idempotent, wijzigt nooit stiekem
  taakstatus).
- **UI** (Next.js) — energiepaneel, takeninbox, proportionele dagtijdlijn met
  kleurcodering (blauw = vaste afspraak, groen→roodbruin = licht→zwaar).
- **Weekweergave** — voorbije dagen én vandaag tonen twee lanes naast elkaar:
  links Moneybird-uren (aangevuld met agenda-events voor vandaag), rechts
  eigen afgeronde taken als echte tijdblokjes. Volle rand = gemeten (via de
  Start-knop of achteraf bevestigd), gestreepte rand = een afleiding (vorige
  taak se klaar-tijd, of een tijdvak-hint) — klikbaar om de echte starttijd
  alsnog vast te leggen. Latere dagen tonen alleen vaste afspraken; taken
  plannen kan voorlopig alleen voor vandaag (zie de "Dag"-weergave).
- **Google Calendar read-only sync** — werkt, inclusief opruimen van lokaal
  gesynchte events die in Google zelf verwijderd zijn.
- **Moneybird-sync** — werkt; tijdzone-conversie was aanvankelijk fout
  (tijdzone-info zomaar afgeknipt i.p.v. eerst naar lokale tijd geconverteerd,
  zelfde valkuil als destijds bij `/plan` — nu ook hier via een
  `_as_local_naive`-achtige stap opgelost).
- **Toegang** — draait puur lokaal; via Tailscale ook bereikbaar op de telefoon, ook
  buiten het eigen wifi-netwerk.

## Nog openstaand / bekende beperkingen

- **Weekweergave is niet live over apparaten/tabbladen** — binnen één open
  pagina werkt alles direct (gedeelde React-state, geen refresh nodig na een
  actie), maar er is geen polling of refetch bij het wisselen naar het
  Week-tabblad. Een taak die je op je telefoon afrondt terwijl de Week-tab op
  je laptop al open staat, zie je daar pas na een handmatige refresh. Nog niet
  opgelost — mogelijke aanpak (refetch bij tab-wissel, of periodieke polling)
  besproken maar niet gekozen.
- **Dagdeel-besef is alleen in de NL-inbox opgelost, niet in de scheduler
  zelf** — de oorspronkelijke roadmap-beperking ("Avondeten kan overdag
  ingepland worden") gold voor taken die via het gewone formulier of API
  worden aangemaakt zonder expliciete `not_before`/`not_after`. Dat is nog
  steeds zo; alleen taken die via de NL-inbox met een dagdeel-woord worden
  aangemaakt profiteren van de nieuwe vertaling.
- **AI-inbox reasoning-tekst kan de eigen regel verkeerd samenvatten** —
  ontdekt bij het testen van "vanavond boodschappen doen": het resultaat
  (tijdvak vanaf 18:00) was correct, maar de reasoning zei dat de tekst een
  "expliciete kloktijd" bevatte, wat niet klopte (het was een dagdeel-woord).
  Geen functioneel probleem, maar de reasoning-tekst is dus niet blind te
  vertrouwen als audit-trail van wát er precies werd herkend.

## Korte termijn — maakt de dagelijkse tool prettiger, geen AI nodig

- **Weer via Open-Meteo** (al bewezen accuraat, gebruikt in sensorcanvas.arcnode.nl) —
  buiten-taken krijgen een weer-afhankelijk tijdvak (regen/hagel/hitte). Weer is en
  blijft een **suggestie**, nooit een harde overschrijving van je eigen energie-invoer.
- **Snelle energie-invoer op de telefoon** (`/quick`-route in dezelfde Next.js-app) —
  geïnspireerd op een ASS-vriendelijke app die je eerder gebruikte: laagdrempelig,
  even snel LOW/redelijk/wakker intikken onderweg, via Tailscale.

## Middellange termijn — de leerlaag opbouwen

De uiteindelijke visie: jij voert alleen nog energie in + wat je die dag afgerond
wilt hebben, en de agent plant zelf op basis van geleerde patronen. Dat vraagt om
een dataset die je nu al kunt beginnen op te bouwen:

- **`plan_vs_actual`-log** — inmiddels breder dan alleen "wat gepland stond
  vs. wat er echt gebeurde": naast `done`/`cancelled`/`deferred`/
  `duration_changed` loggen nu ook `started`/`paused`/`resumed`/
  `started_at_corrected`. Daarmee is er straks ook zicht op *hoe* een taak
  verliep (met onderbrekingen, achteraf gecorrigeerd, etc.), niet alleen *of*
  'ie gebeurde.
- **Moneybird als extra, historische databron** — werkt, inclusief tijdzone-fix.
  Read-only, één richting (Moneybird → ons), bron blijft onderscheidbaar van
  eigen app-logging (`source`-veld op `TaskEventDB`).
- **Sociale/mentale belasting als aparte as** — naast `energy` (fysiek/cognitief)
  optioneel een `social_load` per taak, met een eigen dagbudget. Eten bij je moeder
  is fysiek licht maar kan sociaal zwaar zijn. Nog niet gebouwd.
- **Statistische patroonherkenning** (nog geen ML) — simpele queries over de log:
  "dinsdagmiddag zware taken → hoog annuleer-percentage", etc. Pas als dit
  tekortschiet wordt een echt lerend model interessant. Nog niet gebouwd, maar de
  log is inmiddels rijker dan toen dit punt geschreven werd.
- **Wekelijkse terugblik** — logica ontdekt de patronen (tellen/groeperen), een klein
  lokaal model verwoordt ze alleen in leesbare taal. "AI aan de rand, logica in het
  midden": het systeem blijft zichtbaar en uitlegbaar in plaats van een black box.
  Nog niet gebouwd.

## Los subsysteem, onafhankelijk te bouwen wanneer je zin hebt

- **Interesse-scraper** — wekelijkse job, lezingen/concerten/exposities binnen een
  max. reisafstand, gefilterd/gerankt op interesses (klein lokaal model: titel/
  beschrijving → past dit?). Losse "voorstellen"-tabel, alleen ingepland door de
  scheduler in weken met veel ruimte, verschuift mee met stress/energie.

## Verder weg — pas als er genoeg data + signaal is

- Een echt lerend energiemodel, gebouwd op de dataset uit de middellange termijn.
- Volledig agentic: jij zegt alleen nog "LOW, wil dit vandaag af" en de agent doet
  de rest, met annuleren/aanpassen als doorlopend feedbacksignaal.

## Openstaand: AI mag ook splittable/duur beïnvloeden

Ontdekt 16 augustus: een uitstel-note kan iets zeggen over hoe een taak
gepland zou moeten worden (bv. "mag in stukjes") dat verder gaat dan
prioriteit/tijdvak — maar de AI-laag mag momenteel alleen die twee dingen
voorstellen. `splittable`, duur en andere taakeigenschappen liggen vast bij
aanmaken. Bewust nog niet uitgebreid, om niet weer een nieuwe onbetrouwbaarheid
bovenop een net gestabiliseerd AI-onderdeel te stapelen. Workaround voor nu:
taak annuleren en opnieuw aanmaken met het juiste vinkje.

De NL-inbox (17 augustus) stelt `task_splittable` inmiddels wél voor bij het
aanmaken van een nieuwe taak — dat is een ander moment (aanmaken, niet
achteraf bijstellen) en dus geen tegenspraak met dit punt, maar wel iets om in
de gaten te houden of dezelfde onbetrouwbaarheid zich hier ook gaat voordoen.

## Doorlopend ontwerpprincipe

AI aan de rand, betrouwbare logica in het midden. Elke keer dat een LLM iets
voorstelt (NL-inbox, weer-suggesties, wekelijkse samenvatting), blijft de mens de
laatste stap. Niets schrijft ongevraagd naar de database.

## Bekende beperking: AI-prioriteitsvoorstel (16 augustus, geaccepteerd voorlopig)

Bij het testen van de prioriteits-AI (qwen2.5:7b-instruct) bleek matching van
log-entries aan taken onbetrouwbaar zodra het er veel tegelijk waren — opgelost
door de matching zelf in Python te doen (exacte ID-match) i.p.v. het aan het
model over te laten, zie `ai_priority.py`.

Wat nog steeds inconsistent is, ook na promptaanpassingen:
- Een concrete note ("liever voor 10 uur") wordt soms niet naar
  `preferred_not_after` vertaald, ook als de reasoning-tekst 'm wel correct
  parafraseert. Reasoning en structured output lopen dan uit de pas.
- Subtielere signalen (bv. "twee keer wandelen is teveel" → tweede instantie
  verdient lagere prioriteit) worden niet altijd opgepikt; het model
  herkent de letterlijke tekst maar trekt er niet altijd de juiste conclusie
  voor prioriteit uit.

Besluit: voorlopig accepteren, niet blind verder prompt-tunen op een steekproef
van één scenario. Als dit patroon zich herhaalt of ook bij andere soorten
taken/notes optreedt, geeft dat pas genoeg datapunten om gericht te verbeteren
(of alsnog een groter model te overwegen — `debug_ollama.py` ondersteunt dat al
via een command-line argument).
