"""依分組或目的地的設定過濾航班。"""

from __future__ import annotations

from .config import Target
from .providers.base import Flight


def passes(f: Flight, t: Target) -> bool:
    if f.stops > t.max_stops:
        return False
    if t.max_total_duration_h is not None and f.duration_min > t.max_total_duration_h * 60:
        return False
    if t.max_layover_h is not None and any(m > t.max_layover_h * 60 for m in f.layovers_min):
        return False
    if t.min_layover_min is not None and any(m < t.min_layover_min for m in f.layovers_min):
        return False
    return True


def cheapest(flights: list[Flight], t: Target) -> Flight | None:
    ok = [f for f in flights if passes(f, t)]
    return min(ok, key=lambda f: (f.price, f.duration_min)) if ok else None
