"""每週摘要：熱力圖、趨勢圖、前 3 名組合、各月份最低價、健康狀態。"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .. import airports
from ..config import Config
from ..links import flights_url
from ..storage.state import State
from . import charts
from .alerts import money, short_date


@dataclass
class Row:
    run_date: date
    dest: str
    mode: str
    depart: date
    ret: date
    origins: str
    price: int

    @property
    def month(self) -> str:
        return self.depart.strftime("%Y-%m")


def _rows(state: State, since: date) -> list[Row]:
    return [
        Row(date.fromisoformat(r["run_date"]), r["dest"], r["mode"], date.fromisoformat(r["depart_date"]),
            date.fromisoformat(r["return_date"]), r["origins"], r["price"])
        for r in state.history_rows(since)
    ]


def _month_min(rows: list[Row]) -> dict[tuple[str, str], Row]:
    out: dict[tuple[str, str], Row] = {}
    for r in rows:
        k = (r.dest, r.month)
        if k not in out or r.price < out[k].price:
            out[k] = r
    return out


def _snapshot(rows: list[Row], on_or_before: date) -> list[Row]:
    """on_or_before 當天（或之前最近一次）執行的資料。"""
    dates = sorted({r.run_date for r in rows if r.run_date <= on_or_before})
    if not dates:
        return []
    return [r for r in rows if r.run_date == dates[-1]]


def _links(r: Row, max_stops: int) -> str:
    out_origin, _, back_origin = r.origins.partition("-")
    back_origin = back_origin or out_origin
    if r.mode == "one_way":
        return (f"[去程]({flights_url(out_origin, r.dest, r.depart, max_stops=max_stops)}) ・ "
                f"[回程]({flights_url(r.dest, back_origin, r.ret, max_stops=max_stops)})")
    return f"[查看]({flights_url(out_origin, r.dest, r.depart, r.ret, max_stops)})"


def build(cfg: Config, state: State, today: date) -> tuple[list[dict], dict[str, bytes]]:
    rows = _rows(state, today - timedelta(days=8 * 7))
    latest = _snapshot(rows, today)
    if not latest:
        return [{"title": "📊 每週摘要", "description": "還沒有任何資料。"}], {}

    stops = {t.dest: t.max_stops for t in cfg.targets()}
    current = _month_min(latest)
    previous = _month_min(_snapshot(rows, today - timedelta(days=7)))

    dests = sorted({k[0] for k in current}, key=lambda d: list(stops).index(d) if d in stops else 99)
    months = sorted({k[1] for k in current})
    files = {
        "heatmap.png": charts.heatmap(
            [f"{airports.name(d)} {d}" for d in dests],
            [f"{int(m[5:])} 月" for m in months],
            [[current[(d, m)].price if (d, m) in current else None for m in months] for d in dests],
            f"各目的地、各出發月份最低組合價格（TWD，{latest[0].run_date}）",
        ),
    }

    weekly: dict[str, dict[str, int]] = {}
    for r in rows:
        week = r.run_date - timedelta(days=r.run_date.weekday())
        key = f"{airports.name(r.dest)} {r.dest}"
        wk = week.strftime("%m/%d")
        weekly.setdefault(key, {})
        weekly[key][wk] = min(weekly[key].get(wk, r.price), r.price)
    files["trend.png"] = charts.trend(
        {k: sorted(v.items()) for k, v in weekly.items()}, "近 8 週各目的地最低組合價格（TWD）"
    )

    # 前 3 名：依低於該月份中位數的幅度排序
    ranked = []
    for (dest, month), r in current.items():
        _, median = state.month_baseline(dest, r.mode, month, before=today)
        pct = (median - r.price) / median * 100 if median else 0
        ranked.append((pct, r))
    ranked.sort(key=lambda x: (-x[0], x[1].price))
    top_lines = []
    for i, (pct, r) in enumerate(ranked[:3], 1):
        extra = f" ・ 比中位數低 {pct:.0f}%" if pct > 0 else ""
        top_lines.append(
            f"**{i}. {airports.name(r.dest)} {r.dest}** {money(r.price)}{extra}\n"
            f"　{short_date(r.depart)} → {short_date(r.ret)} ・ {r.origins} ・ {_links(r, stops.get(r.dest, 0))}"
        )

    fields = [{"name": "🏆 本週前 3 名", "value": "\n".join(top_lines) or "—", "inline": False}]
    for d in dests:
        lines = []
        for m in months:
            r = current.get((d, m))
            if r is None:
                continue
            prev = previous.get((d, m))
            change = ""
            if prev and prev.run_date != r.run_date and prev.price != r.price:
                diff = r.price - prev.price
                change = f" {'▲' if diff > 0 else '▼'}{abs(diff):,}"
            lines.append(f"{int(m[5:])} 月 {r.price:,}{change}")
        fields.append({"name": f"{airports.name(d)} {d}", "value": "\n".join(lines) or "—", "inline": True})
    fields.append({"name": "🩺 健康狀態", "value": _health(state, today), "inline": False})

    embeds = [
        {"title": "📊 每週摘要：各月份最低價", "image": {"url": "attachment://heatmap.png"}, "color": 0x5865F2},
        {"title": "📈 近 8 週趨勢", "image": {"url": "attachment://trend.png"}, "color": 0x5865F2},
        {"title": "📋 本週重點", "fields": fields[:25], "color": 0x5865F2},
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
    diffs = []
    for v in state.verify_rows(since):
        if v["calendar_price"] and v["detail_price"]:
            diffs.append(abs(v["detail_price"] - v["calendar_price"]) / v["calendar_price"] * 100)
    if diffs:
        off = sum(1 for d in diffs if d > 5)
        lines.append(f"日期 API 與明細價格：平均差 {statistics.mean(diffs):.1f}%，{len(diffs)} 筆中 {off} 筆差超過 5%")
    return "\n".join(lines)
