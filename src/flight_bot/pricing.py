"""判斷一個組合是否算便宜。基準單位是 (目的地, 模式, 出發月份)。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import AlertsConfig
from .pairing import Combo
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


def baseline(state: State, combo: Combo, today: date, alerts: AlertsConfig) -> Baseline:
    lowest, median = state.month_baseline(combo.dest, combo.mode, combo.month, before=today)
    start = state.history_start(combo.dest, combo.mode)
    warm = start is not None and (today - start).days >= alerts.warmup_days
    return Baseline(lowest, median, warm)


def evaluate(
    price: int,
    combo: Combo,
    base: Baseline,
    alerts: AlertsConfig,
    price_levels: list[str | None] | None = None,
) -> Evaluation:
    """price_levels：明細查詢得到的 Google 價格等級（單程為去程和回程兩個），沒有明細時為 None。"""
    ev = Evaluation()
    if base.median:
        ev.pct_below_median = (base.median - price) / base.median * 100

    if base.warm:
        if alerts.notify_new_low and base.lowest is not None and price < base.lowest:
            ev.triggers.append("new_low")
        if ev.pct_below_median is not None and ev.pct_below_median >= alerts.below_median_pct:
            ev.triggers.append("below_median")
    if alerts.use_google_price_level and price_levels and all(lv == "low" for lv in price_levels):
        ev.triggers.append("google_low")
    if alerts.max_price is not None:
        cap = alerts.max_price.for_month(combo.depart.month)
        if cap is not None and price <= cap:
            ev.triggers.append("max_price")
    return ev


def should_notify(state: State, combo: Combo, price: int, renotify_drop: int) -> bool:
    last = state.last_notified(combo.key)
    return last is None or price <= last - renotify_drop
