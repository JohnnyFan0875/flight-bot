"""產生今天要執行的掃描任務。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config, Target, Tier


@dataclass(frozen=True)
class Leg:
    """單程模式的一個方向，或來回模式的一組 (出發地, 目的地)。"""

    target: Target
    origin: str
    dest: str

    @property
    def mode(self) -> str:
        return self.target.mode

    @property
    def max_stops(self) -> int:
        return self.target.max_stops

    @property
    def outbound(self) -> bool:
        return self.dest == self.target.dest


@dataclass(frozen=True)
class CalendarTask:
    leg: Leg
    start: date
    end: date
    trip_length: int | None = None  # 來回模式才有


@dataclass(frozen=True)
class SearchTask:
    leg: Leg
    depart: date
    ret: date | None = None
    purpose: str = "scan"  # scan / detail / verify


def depart_range(t: Target, today: date) -> tuple[date, date]:
    return today + timedelta(days=t.search_from_days), today + timedelta(days=t.search_to_days)


def return_leg_range(t: Target, today: date) -> tuple[date, date]:
    """單程模式回程方向要查的日期：最早出發日 + 最短停留，到最晚出發日 + 最長停留。"""
    lo, hi = t.trip_length_days
    return (
        today + timedelta(days=t.search_from_days + lo),
        today + timedelta(days=t.search_to_days + hi),
    )


def split_range(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """把 [start, end] 平均切成每段不超過 max_days 天（含兩端）。"""
    total = (end - start).days + 1
    parts = math.ceil(total / max_days)
    size = math.ceil(total / parts)
    out = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def legs_with_ranges(t: Target, today: date) -> list[tuple[Leg, date, date]]:
    """每個目的地要掃描的方向和日期範圍。來回模式只有去程方向。"""
    out = []
    for origin in t.origins:
        out.append((Leg(t, origin, t.dest), *depart_range(t, today)))
        if t.mode == "one_way":
            out.append((Leg(t, t.dest, origin), *return_leg_range(t, today)))
    return out


def trip_lengths(t: Target, all_lengths: bool) -> list[int]:
    lo, hi = t.trip_length_days
    if all_lengths:
        return list(range(lo, hi + 1))
    return sorted({lo, hi})


def calendar_tasks(cfg: Config, today: date) -> list[CalendarTask]:
    tasks = []
    for t in cfg.targets():
        for leg, start, end in legs_with_ranges(t, today):
            for a, b in split_range(start, end, cfg.scan.calendar_max_days):
                if t.mode == "one_way":
                    tasks.append(CalendarTask(leg, a, b))
                else:
                    for length in trip_lengths(t, all_lengths=True):
                        tasks.append(CalendarTask(leg, a, b, length))
    return tasks


def tier_for(n: int, tiers: list[Tier]) -> Tier | None:
    for tier in tiers:
        if n <= tier.until_days:
            return tier
    return None


def due_today(d: date, today: date, tiers: list[Tier]) -> bool:
    tier = tier_for((d - today).days, tiers)
    if tier is None:
        return False
    return d.toordinal() % tier.every == today.toordinal() % tier.every


def _weekday_ok(t: Target, d: date) -> bool:
    return not t.depart_weekdays or d.weekday() in t.depart_weekdays


def per_date_tasks_for(t: Target, leg: Leg, start: date, end: date, cfg: Config, today: date) -> list[SearchTask]:
    """一段日期範圍改用逐日查詢時，今天輪到的日期。"""
    tasks = []
    d = start
    while d <= end:
        if due_today(d, today, cfg.scan.per_date_tiers) and (not leg.outbound or _weekday_ok(t, d)):
            if t.mode == "one_way":
                tasks.append(SearchTask(leg, d))
            else:
                for length in trip_lengths(t, all_lengths=False):
                    tasks.append(SearchTask(leg, d, d + timedelta(days=length)))
        d += timedelta(days=1)
    return tasks


def per_date_tasks(cfg: Config, today: date) -> list[SearchTask]:
    tasks = []
    for t in cfg.targets():
        for leg, start, end in legs_with_ranges(t, today):
            tasks.extend(per_date_tasks_for(t, leg, start, end, cfg, today))
    return tasks
