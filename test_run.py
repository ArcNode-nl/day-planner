"""
Draai dit lokaal om de scheduler te zien werken, zonder server of UI:

    cd backend
    python -m app.test_run
"""

from datetime import datetime, timedelta

from .dummy_data import sample_day
from .scheduler import build_schedule


def print_schedule(blocks) -> None:
    for b in blocks:
        label = {"calendar": "AGENDA", "task": "TAAK", "buffer": "pauze", "free": "vrij"}[b.kind]
        print(f"{b.start:%H:%M}-{b.end:%H:%M}  [{label:7}] {b.title}")


if __name__ == "__main__":
    now = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
    day_start = now
    day_end = now.replace(hour=18, minute=0)

    events, tasks, energy = sample_day(now)

    print(f"Energie: {energy.level.value} ({energy.note})")
    print(f"Plannen van {day_start:%H:%M} tot {day_end:%H:%M}\n")

    schedule = build_schedule(day_start, day_end, now, events, tasks, energy)
    print_schedule(schedule)

    print("\n--- Simuleer: het is nu 13:15, ClubScout is klaar, Canvas Connect liep uit ---\n")
    now2 = now.replace(hour=13, minute=15)
    for t in tasks:
        if t.id == "t4":
            t.done = True
            t.remaining_min = 0
        if t.id == "t1":
            t.remaining_min = 60  # nog 60 min te gaan i.p.v. gepland

    schedule2 = build_schedule(day_start, day_end, now2, events, tasks, energy)
    print_schedule(schedule2)
