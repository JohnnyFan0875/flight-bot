"""把各日期的最低價組成去回組合。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Target


@dataclass
class Combo:
    target: Target
    depart: date
    ret: date
    out_origin: str  # 去程出發地
    back_origin: str  # 回程抵達地；來回模式等於 out_origin
    price: int
    out_price: int | None = None  # 單程模式的去程、回程價格
    back_price: int | None = None

    @property
    def dest(self) -> str:
        return self.target.dest

    @property
    def mode(self) -> str:
        return self.target.mode

    @property
    def month(self) -> str:
        return self.depart.strftime("%Y-%m")

    @property
    def origins(self) -> str:
        return self.out_origin if self.out_origin == self.back_origin else f"{self.out_origin}-{self.back_origin}"

    @property
    def key(self) -> str:
        return f"{self.dest}|{self.mode}|{self.depart}|{self.ret}|{self.origins}"

    @property
    def stay_days(self) -> int:
        return (self.ret - self.depart).days


# quotes：{出發地: {(depart_date, return_date|None): price}}
Quotes = dict[str, dict[tuple[date, date | None], int]]


def _depart_ok(t: Target, d: date, today: date) -> bool:
    n = (d - today).days
    if n < t.search_from_days or n > t.search_to_days:
        return False
    return not t.depart_weekdays or d.weekday() in t.depart_weekdays


def pair_one_way(t: Target, outbound: Quotes, inbound: Quotes, today: date) -> list[Combo]:
    """outbound：{出發地: 去程價格}；inbound：{回程抵達地: 回程價格}。每組 (去程日, 回程日) 只留最便宜的出發地組合。"""
    lo, hi = t.trip_length_days
    out_by_date: dict[date, list[tuple[int, str]]] = {}
    for origin, quotes in outbound.items():
        for (d, _), price in quotes.items():
            if _depart_ok(t, d, today):
                out_by_date.setdefault(d, []).append((price, origin))
    back_by_date: dict[date, list[tuple[int, str]]] = {}
    for origin, quotes in inbound.items():
        for (d, _), price in quotes.items():
            back_by_date.setdefault(d, []).append((price, origin))

    combos = []
    for d, outs in out_by_date.items():
        for length in range(lo, hi + 1):
            r = d + timedelta(days=length)
            backs = back_by_date.get(r)
            if not backs:
                continue
            best = None
            for out_price, out_origin in outs:
                for back_price, back_origin in backs:
                    if not t.allow_mixed_origins and out_origin != back_origin:
                        continue
                    total = out_price + back_price
                    if best is None or total < best.price:
                        best = Combo(t, d, r, out_origin, back_origin, total, out_price, back_price)
            if best:
                combos.append(best)
    return combos


def pair_round_trip(t: Target, quotes: Quotes, today: date) -> list[Combo]:
    lo, hi = t.trip_length_days
    best: dict[tuple[date, date], Combo] = {}
    for origin, by_date in quotes.items():
        for (d, r), price in by_date.items():
            if r is None or not _depart_ok(t, d, today) or not lo <= (r - d).days <= hi:
                continue
            if (d, r) not in best or price < best[(d, r)].price:
                best[(d, r)] = Combo(t, d, r, origin, origin, price)
    return list(best.values())


def best_per_depart(combos: list[Combo]) -> list[Combo]:
    """每個出發日最便宜的一組，存進歷史記錄。"""
    best: dict[date, Combo] = {}
    for c in combos:
        if c.depart not in best or c.price < best[c.depart].price:
            best[c.depart] = c
    return sorted(best.values(), key=lambda c: c.depart)


@dataclass
class MonthBest:
    combo: Combo
    nearby: int  # 同月份另有幾組在最低價 +10% 以內
    nearby_price: int  # 上述的價格上限（取整到百位）


def best_per_month(combos: list[Combo]) -> dict[str, MonthBest]:
    by_month: dict[str, list[Combo]] = {}
    for c in combos:
        by_month.setdefault(c.month, []).append(c)
    out = {}
    for month, items in by_month.items():
        items.sort(key=lambda c: (c.price, c.depart))
        best = items[0]
        cap = -(-int(best.price * 1.1) // 100) * 100
        out[month] = MonthBest(best, sum(1 for c in items[1:] if c.price <= cap), cap)
    return out
