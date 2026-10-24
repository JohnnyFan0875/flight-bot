"""即時降價訊息。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import airports
from ..links import flights_url
from ..pairing import Combo, MonthBest
from ..pricing import TRIGGER_ORDER, Evaluation
from ..providers.base import Flight

COLORS = {
    "new_low": 0x2ECC71,  # 🟢
    "below_median": 0x3498DB,  # 🔵
    "google_low": 0xF1C40F,  # 🟡
    "max_price": 0x9B59B6,
}
WEEKDAYS = "一二三四五六日"
LEVELS = {"low": "偏低", "typical": "一般", "high": "偏高"}


@dataclass
class Alert:
    combo: Combo
    price: int
    evaluation: Evaluation
    month_best: MonthBest | None = None
    out_flight: Flight | None = None  # 來回模式只有這個
    back_flight: Flight | None = None
    out_price: int | None = None
    back_price: int | None = None
    levels: list[str | None] = field(default_factory=list)

    @property
    def top_trigger(self) -> str:
        return min(self.evaluation.triggers, key=TRIGGER_ORDER.index)


def money(n: int | float) -> str:
    return f"TWD {int(n):,}"


def short_date(d) -> str:
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def _title(a: Alert) -> str:
    c = a.combo
    month = f"{c.depart.month} 月"
    label = {
        "new_low": f"{month}新低",
        "below_median": f"{month}低於中位數",
        "google_low": "Google 評為偏低",
        "max_price": "低於設定價格",
    }[a.top_trigger]
    return f"✈️ {airports.name(c.dest)} {c.dest} ｜ {label}"


def _leg_lines(label: str, d, origin: str, dest: str, f: Flight | None) -> list[str]:
    if f is None:
        return [f"{label}  {short_date(d)} {origin} → {dest}"]
    stops = "直飛" if f.stops == 0 else f"轉機 {f.stops} 次（{'、'.join(s.to_airport for s in f.segments[:-1])}）"
    carriers = "、".join(airports.airline(a) for a in f.airlines)
    hours, mins = divmod(f.duration_min, 60)
    return [
        f"{label}  {short_date(d)} {origin} {f.depart_at:%H:%M} → {dest} {f.arrive_at:%H:%M}",
        f"　　　{carriers} {' '.join(f.flight_nos)} ・ {stops} ・ {hours}h{mins:02d}m",
    ]


def build_embed(a: Alert) -> dict:
    c = a.combo
    lines = []
    if c.mode == "one_way" and a.out_price is not None and a.back_price is not None:
        lines.append(f"💰 **{money(a.price)}**（去 {a.out_price:,} + 回 {a.back_price:,}）")
    else:
        lines.append(f"💰 **{money(a.price)}**（來回）")

    notes = []
    pct = a.evaluation.pct_below_median
    if pct is not None and pct > 0:
        notes.append(f"比 {c.depart.month} 月中位數低 {pct:.0f}%")
    known = [lv for lv in a.levels if lv]
    if known:
        notes.append("Google：" + " / ".join(LEVELS[lv] for lv in known))
    if notes:
        lines.append("📉 " + " ・ ".join(notes))
    lines.append("")

    if c.mode == "one_way":
        lines += _leg_lines("去程", c.depart, c.out_origin, c.dest, a.out_flight)
        lines += _leg_lines("回程", c.ret, c.dest, c.back_origin, a.back_flight)
    else:
        lines += _leg_lines("去程", c.depart, c.out_origin, c.dest, a.out_flight)
        lines.append(f"回程  {short_date(c.ret)} {c.dest} → {c.back_origin}")
    lines.append(f"停留 {c.stay_days} 天")

    if a.month_best and a.month_best.combo.key == c.key and a.month_best.nearby:
        lines.append(f"\n另有 {a.month_best.nearby} 組 {money(a.month_best.nearby_price)} 以下")

    stops = c.target.max_stops
    if c.mode == "one_way":
        out_url = flights_url(c.out_origin, c.dest, c.depart, max_stops=stops)
        back_url = flights_url(c.dest, c.back_origin, c.ret, max_stops=stops)
        lines.append(f"🔗 [查看去程]({out_url}) ・ [查看回程]({back_url})")
    else:
        lines.append(f"🔗 [查看航班]({flights_url(c.out_origin, c.dest, c.depart, c.ret, stops)})")

    return {
        "title": _title(a),
        "description": "\n".join(lines),
        "color": COLORS[a.top_trigger],
        "footer": {"text": f"{c.target.group} ・ {' '.join(a.evaluation.triggers)}"},
    }


def build_embeds(alerts: list[Alert]) -> list[dict]:
    ordered = sorted(alerts, key=lambda a: (TRIGGER_ORDER.index(a.top_trigger), -(a.evaluation.pct_below_median or 0)))
    return [build_embed(a) for a in ordered]
