"""判斷一個單程航班是否算便宜。基準單位是 (航線, 出發機場, 抵達機場, 出發月份)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import AlertsConfig
from .storage.state import State

# 顯示和顏色的優先順序
TRIGGER_ORDER = ["new_low", "below_median", "google_low", "max_price"]


@dataclass
class Baseline:
    lowest: int | None
    median: float | None
    warm: bool  # 已經過了 warmup 期間


@dataclass
class Evaluation:
    triggers: list[str] = field(default_factory=list)
    pct_below_median: float | None = None

    @property
    def hit(self) -> bool:
        return bool(self.triggers)


def baseline(state: State, route_id: str, origin: str, dest: str, month: str, today: date,
             alerts: AlertsConfig) -> Baseline:
    lowest, median = state.month_baseline(route_id, origin, dest, month, before=today)
    start = state.history_start(route_id)
    warm = start is not None and (today - start).days >= alerts.warmup_days
    return Baseline(lowest, median, warm)


def evaluate(
    price: int,
    day: date,
    base: Baseline,
    alerts: AlertsConfig,
    typical: tuple[int, int] | None = None,
    level: str | None = None,
    cheapest: bool = False,
) -> Evaluation:
    """typical、level：同一天查詢結果的 Google 典型價格區間和價格等級；cheapest：是不是當天最便宜的航班。"""
    ev = Evaluation()
    if base.median:
        ev.pct_below_median = (base.median - price) / base.median * 100

    if base.warm:
        if alerts.notify_new_low and base.lowest is not None and price < base.lowest:
            ev.triggers.append("new_low")
        if ev.pct_below_median is not None and ev.pct_below_median >= alerts.below_median_pct:
            ev.triggers.append("below_median")
    if alerts.use_google_price_level:
        # 有典型區間就逐班比較；沒有的話只看當天最便宜的那班和 Google 的價格等級
        if typical is not None:
            google_low = price < typical[0]
        else:
            google_low = cheapest and level == "low"
        if google_low:
            ev.triggers.append("google_low")
    if alerts.max_price is not None:
        cap = alerts.max_price.for_month(day.month)
        if cap is not None and price <= cap:
            ev.triggers.append("max_price")
    return ev
