"""依航線的設定過濾航班。"""

from __future__ import annotations

from .config import Route
from .providers.base import Flight


def in_window(f: Flight, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    minute = f.depart_at.hour * 60 + f.depart_at.minute
    start, end = window
    if start <= end:
        return start <= minute <= end
    return minute >= start or minute <= end  # 跨午夜，例如 22:00-02:00


def passes(f: Flight, r: Route, outbound: bool = True) -> bool:
    """outbound：去程用 outbound_time、回程用 return_time。"""
    if f.stops > r.max_stops:
        return False
    if r.max_total_duration_h is not None and f.duration_min > r.max_total_duration_h * 60:
        return False
    if r.max_layover_h is not None and any(m > r.max_layover_h * 60 for m in f.layovers_min):
        return False
    if r.min_layover_min is not None and any(m < r.min_layover_min for m in f.layovers_min):
        return False
    if not in_window(f, r.outbound_time if outbound else r.return_time):
        return False
    return True


def matching(flights: list[Flight], r: Route, outbound: bool = True) -> list[Flight]:
    """符合規則的航班，依價格、飛行時間排序。"""
    return sorted((f for f in flights if passes(f, r, outbound)), key=lambda f: (f.price, f.duration_min))
