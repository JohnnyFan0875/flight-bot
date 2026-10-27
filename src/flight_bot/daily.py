"""每日流程：逐日查詢 → 收集各航線符合規則的單程航班 → 判斷 → Discord 總結。

不預先組合去回程；網頁報告依航線的 trip_days 在瀏覽器裡配對。
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import planner, rules
from .config import Config, Route
from .notify import alerts as alert_fmt
from .notify.discord import Discord
from .planner import SearchTask
from .pricing import Baseline, Evaluation, baseline, evaluate
from .providers.base import Flight, Provider, SearchResult
from .runner import Runner
from .storage.raw import RawWriter
from .storage.state import State

log = logging.getLogger(__name__)

# 查詢失敗時沿用之前查到的價格，但超過這麼多天就視為過期、不列出
FRESH_DAYS = 2


def flight_key(f: Flight) -> tuple:
    """同一班飛機在不同次查詢之間的識別：航班號碼和出發時間。"""
    return tuple(f.flight_nos), f.depart_at.isoformat()


@dataclass
class FlightItem:
    flight: Flight
    evaluation: Evaluation
    prev_price: int | None = None  # 前一次查到同一班的價格
    seen_before: bool = False  # 前一次查詢有沒有這一班

    @property
    def price(self) -> int:
        return self.flight.price

    @property
    def status(self) -> str:
        """new / down / up / same：和這個日期前一次查詢比較。"""
        if not self.seen_before:
            return "new"
        if self.prev_price is None or self.prev_price == self.price:
            return "same"
        return "down" if self.price < self.prev_price else "up"


@dataclass
class LegDay:
    """一條航線、一個方向、一組機場、一個日期：符合規則的所有單程航班。"""

    route: Route
    origin: str
    dest: str
    day: date
    outbound: bool
    flights: list[FlightItem]  # 依價格排序
    base: Baseline
    level: str | None = None  # Google 價格等級
    typical: tuple[int, int] | None = None
    queried_at: datetime | None = None
    run_date: date | None = None  # 哪一天的執行查到的
    max_stops: int = 0  # 查詢時用的轉機上限（Google Flights 連結用）

    @property
    def month(self) -> str:
        return self.day.strftime("%Y-%m")

    @property
    def cheapest(self) -> FlightItem:
        return self.flights[0]

    @property
    def hits(self) -> list[FlightItem]:
        return [f for f in self.flights if f.evaluation.hit]

    @property
    def place(self) -> str:
        """日本那一端的機場。"""
        return self.dest if self.outbound else self.origin


@dataclass
class DailyReport:
    scan_tasks: int = 0
    legs: list[LegDay] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def flights(self) -> int:
        return sum(len(leg.flights) for leg in self.legs)

    @property
    def hits(self) -> list[tuple[LegDay, FlightItem]]:
        return [(leg, f) for leg in self.legs for f in leg.hits]


class Daily:
    def __init__(self, cfg: Config, provider: Provider, state: State, raw: RawWriter,
                 discord: Discord, runner: Runner, today: date, run_id: str, seed: int | None = None):
        self.cfg = cfg
        self.provider = provider
        self.state = state
        self.raw = raw
        self.discord = discord
        self.runner = runner
        self.today = today
        self.run_id = run_id
        self.rng = random.Random(seed)
        self.started = datetime.now(timezone.utc)
        self.report = DailyReport()
        self._bases: dict[tuple, Baseline] = {}

    # ---------- 查詢 ----------

    def scan(self) -> None:
        tasks = planner.per_date_tasks(self.cfg, self.today)
        self.report.scan_tasks = len(tasks)
        self._run_searches(tasks)
        self.state.commit()

    def _run_searches(self, tasks: list[SearchTask]) -> None:
        if not tasks:
            return
        for o in self.runner.run("scan", tasks, self._search_one, is_empty=lambda r: not r.flights):
            t = o.task
            if o.ok:
                self._save_search(t, o.result)
            else:
                self.raw.add_search("scan", t.origin, t.dest, t.depart, None, o.error)

    def _search_one(self, task: SearchTask) -> SearchResult:
        return self.provider.search(task.origin, task.dest, task.depart, None, task.max_stops)

    def _save_search(self, task: SearchTask, result: SearchResult) -> None:
        self.raw.add_search("scan", task.origin, task.dest, task.depart, result)
        flights = [f.to_dict() for f in result.flights if f.stops <= task.max_stops]
        self.state.upsert_search(task.origin, task.dest, task.depart, task.max_stops, result.price_level,
                                 result.typical_range, flights, self.today)

    # ---------- 收集與判斷 ----------

    def _fresh_since(self) -> datetime:
        return self.started - timedelta(days=FRESH_DAYS)

    def _base(self, r: Route, origin: str, dest: str, month: str) -> Baseline:
        key = (r.id, origin, dest, month)
        if key not in self._bases:
            self._bases[key] = baseline(self.state, r.id, origin, dest, month, self.today, self.cfg.alerts)
        return self._bases[key]

    def collect(self) -> list[LegDay]:
        """每條航線、每個方向、每個日期符合規則的航班，逐班和歷史價格比較。"""
        fresh_since = self._fresh_since()
        legs = []
        for r in self.cfg.routes():
            for origin, dest, outbound in r.legs():
                start, end = r.date_range(outbound, self.today)
                for row in self.state.load_searches(origin, dest, start, end, fresh_since):
                    leg = self._leg_day(r, origin, dest, outbound, row)
                    if leg:
                        legs.append(leg)
        legs.sort(key=lambda leg: (leg.route.id, leg.day, not leg.outbound, leg.origin, leg.dest))
        self.report.legs = legs
        return legs

    def _leg_day(self, r: Route, origin: str, dest: str, outbound: bool, row) -> LegDay | None:
        flights = rules.matching([Flight.from_dict(d) for d in json.loads(row["flights_json"])], r)
        if not flights:
            return None
        day = date.fromisoformat(row["depart_date"])
        prev = None
        if row["prev_flights_json"] is not None:
            prev = {flight_key(f): f.price for f in map(Flight.from_dict, json.loads(row["prev_flights_json"]))}
        typical = (row["typical_low"], row["typical_high"]) if row["typical_low"] is not None else None
        base = self._base(r, origin, dest, day.strftime("%Y-%m"))
        items = []
        for k, f in enumerate(flights):
            ev = evaluate(f.price, day, base, self.cfg.alerts, typical, row["price_level"], cheapest=k == 0)
            key = flight_key(f)
            items.append(FlightItem(f, ev, prev.get(key) if prev else None, bool(prev) and key in prev))
        return LegDay(r, origin, dest, day, outbound, items, base, row["price_level"], typical,
                      datetime.fromisoformat(row["queried_at"]), date.fromisoformat(row["run_date"]),
                      row["max_stops"])

    def record_history(self, legs: list[LegDay]) -> None:
        rows = [(leg.route.id, leg.origin, leg.dest, leg.day, leg.cheapest.price) for leg in legs]
        self.state.add_history(self.today, rows)
        self.state.commit()

    # ---------- 通知 ----------

    def notify(self, legs: list[LegDay]) -> None:
        self.discord.send(embeds=alert_fmt.build_summary(legs, self.cfg.routes(), self.today, self.cfg.report.url))

    # ---------- 健康檢查 ----------

    def health(self) -> list[str]:
        stats = self.runner.stats
        warnings = []
        rate = stats.failure_rate()
        if stats.total() and rate > self.cfg.health.max_failure_rate:
            warnings.append(f"本次查詢失敗率 {rate:.0%}（可能被 Google 擋了，或介面改版）")
        if stats.aborted:
            warnings.append(f"連續失敗 {self.cfg.runner.abort_after_consecutive_failures} 次，已提前結束")
        # 沒有要查的日期（航線全部停用或日期都過了）就不算異常
        if self.report.scan_tasks and stats.total("ok") + stats.total("empty") == 0:
            last = self.state.last_success()
            days = self.cfg.health.max_days_without_success
            if last is None or datetime.now(timezone.utc) - last >= timedelta(days=days):
                warnings.append(f"已經 {days} 天以上沒有任何成功的查詢")
        if warnings:
            streak = 1
            for run in reversed(self.state.recent_runs("daily", self.started - timedelta(days=14))):
                total = run["ok"] + run["failed"] + run["empty"]
                bad = run["aborted"] or (total and run["failed"] / total > self.cfg.health.max_failure_rate)
                if not bad:
                    break
                streak += 1
            if streak > 1:
                warnings.append(f"已連續 {streak} 次執行異常")
        self.report.warnings = warnings
        return warnings

    # ---------- 全部流程 ----------

    def run(self) -> DailyReport:
        self.scan()
        legs = self.collect()
        self.record_history(legs)
        self.notify(legs)
        warnings = self.health()
        if warnings:
            self.discord.send(content="**flight-bot 異常**\n" + "\n".join(warnings))
        return self.report
