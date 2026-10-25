"""每日網頁報告：資料整理成 JSON，嵌在單一 HTML 檔裡，由頁面上的 JavaScript 配對、篩選、排序。

資料只有單程航班（每條航線、每個方向、每個日期符合規則的全部航班），
去回程由頁面依航線的 trip_days 配對。只放頁面用得到的欄位，避免報告太大。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import airports
from .config import Config
from .daily import FlightItem, LegDay
from .links import flights_url
from .notify.alerts import WEEKDAYS
from .runner import Stats

TAIPEI = timezone(timedelta(hours=8))
TEMPLATE = Path(__file__).with_name("report_template.html")


def _dow(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def _flight(item: FlightItem) -> dict:
    f = item.flight
    return {
        "dep": f.depart_at.strftime("%H:%M"),
        "arr": f.arrive_at.strftime("%H:%M"),
        "off": (f.arrive_at.date() - f.depart_at.date()).days,
        "nos": f.flight_nos,
        "al": [airports.airline(a) for a in f.airlines],
        "stops": f.stops,
        "dur": f.duration_min,
        "price": f.price,
        "st": item.status,
        "prev": item.prev_price,
    }


def _leg(leg: LegDay, route_index: int) -> dict:
    return {
        "r": route_index,
        "o": leg.origin,
        "d": leg.dest,
        "out": leg.outbound,
        "date": leg.day.isoformat(),
        "dow": _dow(leg.day),
        "url": flights_url(leg.origin, leg.dest, leg.day, None, leg.max_stops),
        "flights": [_flight(f) for f in leg.flights],
    }


def build_data(cfg: Config, report, stats: Stats, run_id: str, run_date: date,
               started: datetime, finished: datetime, coverage_note: str | None,
               history: list[str]) -> dict:
    routes = cfg.routes()
    index = {r.id: k for k, r in enumerate(routes)}
    places = {code for r in routes for code in (*r.origins, *r.dests)}
    return {
        "run": {
            "id": run_id,
            "date": run_date.isoformat(),
            "dow": _dow(run_date),
            "generated_at": finished.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M"),
            "duration_min": round((finished - started).total_seconds() / 60, 1),
            "requests": stats.by_kind(),
            "total": stats.total(),
            "failed": stats.total("failed"),
            "failure_rate": round(stats.failure_rate() * 100, 1),
            "aborted": stats.aborted,
            "warnings": report.warnings,
            "note": coverage_note,
        },
        "airports": {code: airports.name(code) for code in sorted(places)},
        "routes": [{
            "id": r.id,
            "name": r.name,
            "origins": list(r.origins),
            "dests": list(r.dests),
            "days": list(r.trip_days),
            "range": [r.query_start.isoformat(), r.query_end.isoformat()],
        } for r in routes],
        "legs": [_leg(leg, index[leg.route.id]) for leg in report.legs],
        "history": history,
    }


def render(data: dict, base: str = "") -> str:
    """base：歷史報告連結的前綴；首頁是 ""，歷史頁面（在 d/ 底下）是 "../"。"""
    payload = json.dumps({**data, "base": base}, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")  # 避免資料裡的字串提早結束 <script>
    title = f"機票總覽 ({data['run']['date'].replace('-', '/')})"
    return TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", title).replace("__DATA__", payload)
