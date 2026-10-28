"""Discord 每日總結（完整列表在網頁報告）。"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..pricing import TRIGGER_ORDER

if TYPE_CHECKING:
    from ..daily import LegDay

COLORS = {
    "new_low": 0x2ECC71,  # 綠
    "below_median": 0x3498DB,  # 藍
    "google_low": 0xF1C40F,  # 黃
    "max_price": 0x9B59B6,
}
NO_ALERT_COLOR = 0x95A5A6
WEEKDAYS = "一二三四五六日"


def money(n: int | float) -> str:
    return f"TWD {int(n):,}"


def short_date(d) -> str:
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def build_summary(legs: list[LegDay], run_date: date, url: str | None) -> list[dict]:
    """每日總結：只有符合條件的班數和報告連結；各航線的價格看網頁報告。"""
    hits = [(leg, f) for leg in legs for f in leg.hits]
    new = sum(1 for _, f in hits if f.status == "new")
    down = sum(1 for _, f in hits if f.status == "down")

    lines = []
    if hits:
        lines.append(f"符合條件的單程航班 **{len(hits)}** 班，新出現 {new} 班、降價 {down} 班")
    else:
        lines.append("今天沒有符合條件的航班")
    if url:
        lines += ["", f"[開啟完整報告]({url})"]

    tops = [min(f.evaluation.triggers, key=TRIGGER_ORDER.index) for _, f in hits]
    color = COLORS[min(tops, key=TRIGGER_ORDER.index)] if tops else NO_ALERT_COLOR
    head = {
        "title": f"每日報告 {run_date.month}/{run_date.day}（{WEEKDAYS[run_date.weekday()]}）",
        "description": "\n".join(lines),
        "color": color,
    }
    if url:
        head["url"] = url
    return [head]
