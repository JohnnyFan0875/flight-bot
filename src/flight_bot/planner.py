"""產生今天要執行的逐日查詢：每條航線的日期範圍內每天都查。
同一組 (出發機場, 抵達機場) 不管出現在幾條航線，同一個日期只查一次。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config


@dataclass(frozen=True)
class SearchTask:
    origin: str
    dest: str
    depart: date
    max_stops: int


def per_date_tasks(cfg: Config, today: date) -> list[SearchTask]:
    # 同一組機場、同一天，用各航線轉機上限的最大值查一次，各航線再依自己的規則過濾
    stops: dict[tuple[str, str, date], int] = {}
    for r in cfg.routes():
        for origin, dest, outbound in r.legs():
            start, end = r.date_range(outbound, today)
            d = start
            while d <= end:
                key = (origin, dest, d)
                stops[key] = max(stops.get(key, 0), r.max_stops)
                d += timedelta(days=1)
    return [SearchTask(o, d, day, n) for (o, d, day), n in stops.items()]
