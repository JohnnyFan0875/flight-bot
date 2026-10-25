"""每週摘要：熱力圖、趨勢圖、前 3 名單程、各月份最低價、健康狀態。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .. import airports
from ..config import Config, Route
from ..links import flights_url
from ..storage.state import State
from . import charts
from .alerts import money, short_date


@dataclass
class Row:
    run_date: date
    route: Route
    origin: str
    dest: str
    depart: date
    price: int

    @property
    def month(self) -> str:
        return self.depart.strftime("%Y-%m")

    @property
    def outbound(self) -> bool:
        return self.origin in self.route.origins

    @property
    def series(self) -> tuple[str, bool]:
        """熱力圖和趨勢圖的一列：(航線, 是否為去程)。"""
        return self.route.id, self.outbound


def _rows(state: State, since: date, routes: dict[str, Route]) -> list[Row]:
    # 只看目前 config 裡的航線；拿掉的航線資料仍保留在 state，加回來時可以延續
    return [
        Row(date.fromisoformat(r["run_date"]), routes[r["route"]], r["origin"], r["dest"],
            date.fromisoformat(r["depart_date"]), r["price"])
        for r in state.history_rows(since)
        if r["route"] in routes
    ]


def _month_min(rows: list[Row]) -> dict[tuple, Row]:
    """{(航線, 是否去程, 月份): 最低價}。"""
    out: dict[tuple, Row] = {}
    for r in rows:
        k = (*r.series, r.month)
        if k not in out or r.price < out[k].price:
            out[k] = r
    return out


def _snapshot(rows: list[Row], on_or_before: date) -> list[Row]:
    """on_or_before 當天（或之前最近一次）執行的資料。"""
    dates = sorted({r.run_date for r in rows if r.run_date <= on_or_before})
    if not dates:
        return []
    return [r for r in rows if r.run_date == dates[-1]]


def _label(routes: dict[str, Route], series: tuple[str, bool]) -> str:
    return f"{routes[series[0]].name} {'去程' if series[1] else '回程'}"


def build(cfg: Config, state: State, today: date) -> tuple[list[dict], dict[str, bytes]]:
    routes = {r.id: r for r in cfg.routes()}
    rows = _rows(state, today - timedelta(days=8 * 7), routes)
    latest = _snapshot(rows, today)
    if not latest:
        return [{"title": "每週摘要", "description": "還沒有任何資料。"}], {}

    current = _month_min(latest)
    previous = _month_min(_snapshot(rows, today - timedelta(days=7)))
    order = list(routes)
    series = sorted({k[:2] for k in current}, key=lambda s: (order.index(s[0]), not s[1]))
    months = sorted({k[2] for k in current})
    files = {
        "heatmap.png": charts.heatmap(
            [_label(routes, s) for s in series],
            [f"{int(m[5:])} 月" for m in months],
            [[current[(*s, m)].price if (*s, m) in current else None for m in months] for s in series],
            f"各航線、各出發月份最低單程價格（TWD，{latest[0].run_date}）",
        ),
    }

    weekly: dict[str, dict[str, int]] = {}
    for r in rows:
        week = (r.run_date - timedelta(days=r.run_date.weekday())).strftime("%m/%d")
        line = weekly.setdefault(_label(routes, r.series), {})
        line[week] = min(line.get(week, r.price), r.price)
    files["trend.png"] = charts.trend(
        {k: sorted(v.items()) for k, v in weekly.items()}, "近 8 週各航線最低單程價格（TWD）"
    )

    # 前 3 名：每組 (航線, 機場, 月份) 最便宜的一天，依低於該月份中位數的幅度排序
    best: dict[tuple, Row] = {}
    for r in latest:
        k = (r.route.id, r.origin, r.dest, r.month)
        if k not in best or r.price < best[k].price:
            best[k] = r
    ranked = []
    for (route_id, origin, dest, month), r in best.items():
        _, median = state.month_baseline(route_id, origin, dest, month, before=today)
        pct = (median - r.price) / median * 100 if median else 0
        ranked.append((pct, r))
    ranked.sort(key=lambda x: (-x[0], x[1].price))
    top_lines = []
    for i, (pct, r) in enumerate(ranked[:3], 1):
        extra = f" ・ 比中位數低 {pct:.0f}%" if pct > 0 else ""
        url = flights_url(r.origin, r.dest, r.depart, max_stops=r.route.max_stops)
        top_lines.append(
            f"**{i}. {r.route.name}** {r.origin}→{r.dest} {money(r.price)}{extra}\n"
            f"　{short_date(r.depart)} {airports.name(r.origin)}→{airports.name(r.dest)} ・ [查看]({url})"
        )

    fields = [{"name": "本週前 3 名（單程）", "value": "\n".join(top_lines) or "—", "inline": False}]
    for s in series:
        lines = []
        for m in months:
            r = current.get((*s, m))
            if r is None:
                continue
            prev = previous.get((*s, m))
            change = ""
            if prev and prev.run_date != r.run_date and prev.price != r.price:
                diff = r.price - prev.price
                change = f" {'▲' if diff > 0 else '▼'}{abs(diff):,}"
            lines.append(f"{int(m[5:])} 月 {r.price:,}{change}")
        fields.append({"name": _label(routes, s), "value": "\n".join(lines) or "—", "inline": True})
    fields.append({"name": "健康狀態", "value": _health(state, today), "inline": False})

    embeds = [
        {"title": "每週摘要：各月份最低單程價", "image": {"url": "attachment://heatmap.png"}, "color": 0x5865F2},
        {"title": "近 8 週趨勢", "image": {"url": "attachment://trend.png"}, "color": 0x5865F2},
        {"title": "本週重點", "fields": fields[:25], "color": 0x5865F2},
    ]
    return embeds, files


def _health(state: State, today: date) -> str:
    since = datetime.combine(today - timedelta(days=7), time(), tzinfo=timezone.utc)
    runs = state.recent_runs("daily", since)
    ok = sum(r["ok"] for r in runs)
    failed = sum(r["failed"] for r in runs)
    empty = sum(r["empty"] for r in runs)
    total = ok + failed + empty
    per_kind: dict[str, int] = {}
    for r in runs:
        for kind, counts in json.loads(r["requests_json"] or "{}").items():
            per_kind[kind] = per_kind.get(kind, 0) + sum(counts.values())
    kinds = "、".join(f"{k} {n}" for k, n in sorted(per_kind.items()))
    lines = [
        f"執行 {len(runs)} 次 ・ 查詢 {total} 次（{kinds or '—'}）",
        f"失敗率 {failed / total:.1%}" if total else "失敗率 —",
    ]
    return "\n".join(lines)
