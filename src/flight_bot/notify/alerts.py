"""Discord 每日總結（完整列表在網頁報告）。"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..config import Route
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
TRIGGER_NAMES = {"new_low": "新低", "below_median": "低於中位數", "google_low": "Google 偏低", "max_price": "低於設定"}


def money(n: int | float) -> str:
    return f"TWD {int(n):,}"


def short_date(d) -> str:
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def fmt_window(window: tuple[int, int] | None) -> str:
    if window is None:
        return "不限"
    (h1, m1), (h2, m2) = divmod(window[0], 60), divmod(window[1], 60)
    return f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}"


def _cheapest_line(label: str, legs: list[LegDay]) -> str:
    if not legs:
        return f"{label} —"
    best = min(legs, key=lambda leg: (leg.cheapest.price, leg.day))
    hits = sum(len(leg.hits) for leg in legs)
    return (f"{label} {money(best.cheapest.price)} 起（{short_date(best.day)} {best.origin}→{best.dest}）"
            + (f" ・ 符合 {hits} 班" if hits else ""))


def build_summary(legs: list[LegDay], routes: list[Route], run_date: date, url: str | None,
                  coverage_note: str | None = None) -> dict:
    """每日總結：每條航線去程、回程各一行最低單程價和符合條件的班數，完整列表看網頁報告。"""
    hits = [(leg, f) for leg in legs for f in leg.hits]
    counts = {t: sum(1 for _, f in hits if t in f.evaluation.triggers) for t in TRIGGER_ORDER}
    new = sum(1 for _, f in hits if f.status == "new")
    down = sum(1 for _, f in hits if f.status == "down")

    lines = []
    if hits:
        detail = "、".join(f"{TRIGGER_NAMES[t]} {n}" for t, n in counts.items() if n)
        lines.append(f"符合條件的單程航班 **{len(hits)}** 班，新出現 {new} 班、降價 {down} 班")
        lines.append(f"（{detail}）")
    else:
        lines.append("今天沒有符合條件的航班")
    lines.append("")

    for r in routes:
        mine = [leg for leg in legs if leg.route.id == r.id]
        lines.append(f"**{r.name}**")
        lines.append(_cheapest_line("　去程", [leg for leg in mine if leg.outbound]))
        lines.append(_cheapest_line("　回程", [leg for leg in mine if not leg.outbound]))

    if coverage_note:
        lines += ["", coverage_note]
    if url:
        lines += ["", f"[開啟完整報告]({url})"]

    tops = [min(f.evaluation.triggers, key=TRIGGER_ORDER.index) for _, f in hits]
    color = COLORS[min(tops, key=TRIGGER_ORDER.index)] if tops else NO_ALERT_COLOR
    embed = {
        "title": f"每日報告 {run_date.month}/{run_date.day}（{WEEKDAYS[run_date.weekday()]}）",
        "description": "\n".join(lines),
        "color": color,
    }
    if url:
        embed["url"] = url
    return embed
