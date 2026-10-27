"""Discord 每日總結（完整列表在網頁報告）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from ..config import Route
from ..pricing import TRIGGER_ORDER

if TYPE_CHECKING:
    from ..daily import FlightItem, LegDay

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


NOON = 12  # 起飛時間 12:00 以前算上午，之後算下午


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _low(items: list[FlightItem]) -> str:
    """最便宜的價格，靠右對齊成固定寬度（放在 code block 裡用等寬字型對齊）。"""
    return (f"{min(f.price for f in items):,}" if items else "-").rjust(6)


def _direction(items: list[FlightItem]) -> str:
    """上午 / 下午起飛最便宜的價格。"""
    am = [f for f in items if f.flight.depart_at.hour < NOON]
    pm = [f for f in items if f.flight.depart_at.hour >= NOON]
    return f"{_low(am)} / {_low(pm)}"


def _week_label(start: date) -> str:
    end = start + timedelta(days=6)
    return f"{start.month}/{start.day}–" + (f"{end.day}" if end.month == start.month else f"{end.month}/{end.day}")


def _route_embed(r: Route, legs: list[LegDay]) -> dict:
    """一條航線：依出發日期分週，每週三行：週次、去程、回程，各列上午 / 下午起飛最便宜的價格。"""
    weeks: dict[date, list[LegDay]] = {}
    for leg in legs:
        weeks.setdefault(_week_start(leg.day), []).append(leg)
    lines = []
    for start in sorted(weeks):
        out = [f for leg in weeks[start] if leg.outbound for f in leg.flights]
        back = [f for leg in weeks[start] if not leg.outbound for f in leg.flights]
        lines += [_week_label(start), f"  去 {_direction(out)}", f"  回 {_direction(back)}"]
    hits = [f for leg in legs for f in leg.hits]
    tops = [min(f.evaluation.triggers, key=TRIGGER_ORDER.index) for f in hits]
    return {
        "title": f"{r.name}" + (f"（符合 {len(hits)} 班）" if hits else ""),
        "description": "```\n" + "\n".join(lines) + "\n```" if lines else "沒有航班資料",
        "color": COLORS[min(tops, key=TRIGGER_ORDER.index)] if tops else NO_ALERT_COLOR,
    }


def build_summary(legs: list[LegDay], routes: list[Route], run_date: date, url: str | None) -> list[dict]:
    """每日總結：第一個 embed 是符合條件的班數，之後每條航線一個 embed，列出每週上午、下午最低價。"""
    hits = [(leg, f) for leg in legs for f in leg.hits]
    new = sum(1 for _, f in hits if f.status == "new")
    down = sum(1 for _, f in hits if f.status == "down")

    lines = []
    if hits:
        lines.append(f"符合條件的單程航班 **{len(hits)}** 班，新出現 {new} 班、降價 {down} 班")
    else:
        lines.append("今天沒有符合條件的航班")
    lines.append("各航線每週最低單程價格（TWD）：上午 / 下午 起飛")
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
    return [head] + [_route_embed(r, [leg for leg in legs if leg.route.id == r.id]) for r in routes]
