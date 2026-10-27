"""依航線的設定過濾航班。"""

from __future__ import annotations

from .config import Route
from .providers.base import Flight


def passes(f: Flight, r: Route) -> bool:
    if f.stops > r.max_stops:
        return False
    if r.max_total_duration_h is not None and f.duration_min > r.max_total_duration_h * 60:
        return False
    if r.max_layover_h is not None and any(m > r.max_layover_h * 60 for m in f.layovers_min):
        return False
    if r.min_layover_min is not None and any(m < r.min_layover_min for m in f.layovers_min):
        return False
    return True


def matching(flights: list[Flight], r: Route) -> list[Flight]:
    """符合規則的航班，依價格、飛行時間排序。"""
    return sorted((f for f in flights if passes(f, r)), key=lambda f: (f.price, f.duration_min))
