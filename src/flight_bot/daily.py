"""每日流程：掃描 → 配對 → 挑候選 → 明細查詢 → 判斷 → 通知。"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import planner, rules
from .config import Config, Target
from .notify import alerts as alert_fmt
from .notify.discord import Discord
from .pairing import Combo, MonthBest, best_per_depart, best_per_month, pair_one_way, pair_round_trip
from .planner import CalendarTask, Leg, SearchTask
from .pricing import Baseline, baseline, evaluate, should_notify
from .providers.base import CalendarQuote, Flight, Provider, SearchResult
from .runner import Runner
from .storage.raw import RawWriter
from .storage.state import State

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    combo: Combo
    month_best: MonthBest
    base: Baseline
    scan_triggered: bool
    pct: float


@dataclass
class DailyReport:
    calendar_tasks: int = 0
    calendar_failed: int = 0
    fallback_tasks: int = 0
    combos: int = 0
    candidates: int = 0
    detail_tasks: int = 0
    verify_tasks: int = 0
    alerts: list[alert_fmt.Alert] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
        self.calendar_prices: dict[tuple, int | None] = {}  # 今天日期 API 的價格，給驗證用

    # ---------- 掃描 ----------

    def scan(self) -> None:
        if self.cfg.scan.strategy == "calendar":
            self._scan_calendar()
        else:
            self._run_searches(planner.per_date_tasks(self.cfg, self.today))
        self.state.commit()

    def _scan_calendar(self) -> None:
        tasks = planner.calendar_tasks(self.cfg, self.today)
        self.report.calendar_tasks = len(tasks)
        # 日期 API 有逐日查詢當備案，所以它的失敗不會讓整次執行提前結束；
        # 先試 1 次，失敗就不再打剩下的請求，連續失敗 3 次也放棄剩下的
        run = lambda batch: self.runner.run(  # noqa: E731
            "calendar", batch, self._calendar_one,
            is_empty=lambda quotes: all(q.price is None for q in quotes),
            give_up_after=3, can_abort=False,
        )
        outcomes = run(tasks[:1])
        if outcomes and outcomes[0].ok:
            outcomes += run(tasks[1:])
        succeeded = set()
        for o in outcomes:
            if o.ok:
                succeeded.add(o.task)
                self._save_calendar(o.task, o.result)
        # 提前結束時沒跑到的任務也算失敗；來回模式同一段日期有多個停留天數，只需備案一次
        failed = {(t.leg, t.start, t.end) for t in tasks if t not in succeeded}
        self.report.calendar_failed = sum(1 for t in tasks if t not in succeeded)

        if failed and self.cfg.scan.fallback_to_per_date and not self.runner.stats.aborted:
            fallback: list[SearchTask] = []
            for leg, start, end in sorted(failed, key=lambda f: (f[0].origin, f[0].dest, f[1])):
                fallback += planner.per_date_tasks_for(leg.target, leg, start, end, self.cfg, self.today)
            self.report.fallback_tasks = len(fallback)
            log.warning("日期 API 有 %d 段失敗，改用逐日查詢 %d 次", len(failed), len(fallback))
            self._run_searches(fallback)

    def _calendar_one(self, task: CalendarTask) -> list[CalendarQuote]:
        leg = task.leg
        return self.provider.calendar(leg.origin, leg.dest, task.start, task.end, leg.max_stops, task.trip_length)

    def _save_calendar(self, task: CalendarTask, quotes: list[CalendarQuote]) -> None:
        leg = task.leg
        rows = []
        for q in quotes:
            rows.append((leg.mode, leg.origin, leg.dest, q.depart_date, q.return_date, q.price, "calendar"))
            self.calendar_prices[(leg.origin, leg.dest, q.depart_date, q.return_date)] = q.price
        self.state.upsert_quotes(rows)
        self.raw.add_calendar(leg.mode, leg.origin, leg.dest, leg.max_stops, quotes)

    # ---------- 逐日查詢（掃描備案、明細、驗證共用） ----------

    def _run_searches(self, tasks: list[SearchTask]) -> None:
        if not tasks:
            return
        by_purpose: dict[str, list[SearchTask]] = {}
        for t in tasks:
            by_purpose.setdefault(t.purpose, []).append(t)
        for purpose, group in by_purpose.items():
            outcomes = self.runner.run(purpose, group, self._search_one, is_empty=lambda r: not r.flights)
            for o in outcomes:
                t = o.task
                if o.ok:
                    self._save_search(t, o.result)
                else:
                    self.raw.add_search(t.purpose, t.leg.mode, t.leg.origin, t.leg.dest, t.depart, t.ret, None, o.error)

    def _search_one(self, task: SearchTask) -> SearchResult:
        leg = task.leg
        return self.provider.search(leg.origin, leg.dest, task.depart, task.ret, leg.max_stops)

    def _save_search(self, task: SearchTask, result: SearchResult) -> None:
        leg = task.leg
        self.raw.add_search(task.purpose, leg.mode, leg.origin, leg.dest, task.depart, task.ret, result)
        best = rules.cheapest(result.flights, leg.target)
        price = best.price if best else None
        self.state.upsert_detail(leg.mode, leg.origin, leg.dest, task.depart, task.ret, price,
                                 result.price_level, best.to_dict() if best else None)
        if task.purpose == "scan":
            self.state.upsert_quotes([(leg.mode, leg.origin, leg.dest, task.depart, task.ret, price, "per_date")])

        key = (leg.origin, leg.dest, task.depart, task.ret)
        if key in self.calendar_prices:
            # 日期 API 只套用轉機數，所以拿同樣條件下的最低價比較
            same_stops = [f.price for f in result.flights if f.stops <= leg.max_stops]
            self.state.log_verify(self.run_id, leg.mode, leg.origin, leg.dest, task.depart, task.ret,
                                  self.calendar_prices[key], min(same_stops) if same_stops else None)

    # ---------- 配對 ----------

    def pair(self) -> dict[Target, list[Combo]]:
        fresh_since = datetime.now(timezone.utc) - timedelta(days=self.cfg.scan.max_every() + 1)
        out = {}
        for t in self.cfg.targets():
            if t.mode == "one_way":
                outbound = {o: self.state.load_quotes(t.mode, o, t.dest, fresh_since) for o in t.origins}
                inbound = {o: self.state.load_quotes(t.mode, t.dest, o, fresh_since) for o in t.origins}
                combos = pair_one_way(t, outbound, inbound, self.today)
            else:
                quotes = {o: self.state.load_quotes(t.mode, o, t.dest, fresh_since) for o in t.origins}
                combos = pair_round_trip(t, quotes, self.today)
            out[t] = combos
            self.report.combos += len(combos)
        return out

    def record_history(self, combos: dict[Target, list[Combo]]) -> None:
        rows = [
            (c.dest, c.mode, c.depart, c.ret, c.origins, c.price)
            for items in combos.values()
            for c in best_per_depart(items)
        ]
        self.state.add_history(self.today, rows)
        self.state.commit()

    # ---------- 候選與明細 ----------

    def candidates(self, combos: dict[Target, list[Combo]]) -> list[Candidate]:
        out = []
        for items in combos.values():
            for mb in best_per_month(items).values():
                c = mb.combo
                base = baseline(self.state, c, self.today, self.cfg.alerts)
                ev = evaluate(c.price, c, base, self.cfg.alerts)
                out.append(Candidate(c, mb, base, ev.hit, ev.pct_below_median or 0.0))
        # 已經觸發的優先，其次是離中位數最遠的（最可能被 Google 評為偏低）
        out.sort(key=lambda x: (not x.scan_triggered, -x.pct, x.combo.price))
        self.report.candidates = len(out)
        return out

    def _detail_tasks_for(self, c: Combo) -> list[SearchTask]:
        t = c.target
        if c.mode == "one_way":
            return [
                SearchTask(Leg(t, c.out_origin, t.dest), c.depart, None, "detail"),
                SearchTask(Leg(t, t.dest, c.back_origin), c.ret, None, "detail"),
            ]
        return [SearchTask(Leg(t, c.out_origin, t.dest), c.depart, c.ret, "detail")]

    def _has_detail_today(self, task: SearchTask) -> bool:
        leg = task.leg
        return self.state.get_detail(leg.mode, leg.origin, leg.dest, task.depart, task.ret, self.started - timedelta(hours=20)) is not None

    def fetch_details(self, cands: list[Candidate]) -> None:
        budget = self.cfg.scan.detail_budget
        tasks: list[SearchTask] = []
        seen = set()
        for cand in cands:
            needed = [t for t in self._detail_tasks_for(cand.combo)
                      if (t.leg, t.depart, t.ret) not in seen and not self._has_detail_today(t)]
            if len(tasks) + len(needed) > budget:
                if cand.scan_triggered:
                    continue  # 這組放不下，看看後面較小的還放不放得下
                break
            for t in needed:
                seen.add((t.leg, t.depart, t.ret))
                tasks.append(t)

        verify = self._verify_tasks(seen)
        self.report.detail_tasks = len(tasks)
        self.report.verify_tasks = len(verify)
        self._run_searches(tasks + verify)
        self.state.commit()

    def _verify_tasks(self, skip: set) -> list[SearchTask]:
        n = self.cfg.scan.verify_samples
        if n == 0 or not self.calendar_prices:
            return []
        legs = {}
        for t in self.cfg.targets():
            for leg, _, _ in planner.legs_with_ranges(t, self.today):
                legs[(leg.origin, leg.dest)] = leg
        pool = [k for k, p in self.calendar_prices.items() if p is not None and (k[0], k[1]) in legs]
        self.rng.shuffle(pool)
        out = []
        for origin, dest, depart, ret in pool:
            leg = legs[(origin, dest)]
            if (leg, depart, ret) in skip:
                continue
            out.append(SearchTask(leg, depart, ret, "verify"))
            if len(out) >= n:
                break
        return out

    # ---------- 判斷與通知 ----------

    def _detail(self, leg: Leg, depart: date, ret: date | None):
        row = self.state.get_detail(leg.mode, leg.origin, leg.dest, depart, ret, self.started - timedelta(hours=20))
        if row is None:
            return None
        flight = Flight.from_dict(json.loads(row["flight_json"])) if row["flight_json"] else None
        return row["price"], row["price_level"], flight

    def build_alerts(self, cands: list[Candidate]) -> list[alert_fmt.Alert]:
        out = []
        for cand in cands:
            c = cand.combo
            t = c.target
            price = c.price
            out_price, back_price = c.out_price, c.back_price
            out_flight = back_flight = None
            levels: list[str | None] = []

            if c.mode == "one_way":
                d_out = self._detail(Leg(t, c.out_origin, t.dest), c.depart, None)
                d_back = self._detail(Leg(t, t.dest, c.back_origin), c.ret, None)
                if (d_out and d_out[0] is None) or (d_back and d_back[0] is None):
                    continue  # 明細查不到符合規則的航班
                if d_out and d_back:
                    out_price, back_price = d_out[0], d_back[0]
                    price = out_price + back_price
                    levels = [d_out[1], d_back[1]]
                out_flight = d_out[2] if d_out else None
                back_flight = d_back[2] if d_back else None
            else:
                d = self._detail(Leg(t, c.out_origin, t.dest), c.depart, c.ret)
                if d and d[0] is None:
                    continue
                if d:
                    price, levels, out_flight = d[0], [d[1]], d[2]

            ev = evaluate(price, c, cand.base, self.cfg.alerts, levels or None)
            if not ev.hit or not should_notify(self.state, c, price, self.cfg.alerts.renotify_drop):
                continue
            out.append(alert_fmt.Alert(c, price, ev, cand.month_best, out_flight, back_flight,
                                       out_price, back_price, levels))
        self.report.alerts = out
        return out

    def notify(self, alerts: list[alert_fmt.Alert]) -> None:
        if not alerts:
            return
        self.discord.send(embeds=alert_fmt.build_embeds(alerts))
        for a in alerts:
            self.state.mark_notified(a.combo.key, a.price)
        self.state.commit()

    # ---------- 健康檢查 ----------

    def health(self) -> list[str]:
        stats = self.runner.stats
        warnings = []
        rate = stats.failure_rate()
        if stats.total() and rate > self.cfg.health.max_failure_rate:
            warnings.append(f"本次查詢失敗率 {rate:.0%}（可能被 Google 擋了，或介面改版）")
        if stats.aborted:
            warnings.append(f"連續失敗 {self.cfg.runner.abort_after_consecutive_failures} 次，已提前結束")
        if self.report.calendar_tasks and self.report.calendar_failed == self.report.calendar_tasks:
            warnings.append("日期價格 API 全部失敗" + ("，已改用逐日查詢" if self.report.fallback_tasks else ""))
        if stats.total("ok") + stats.total("empty") == 0:
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
        combos = self.pair()
        cands = self.candidates(combos)
        self.record_history(combos)
        self.fetch_details(cands)
        alerts = self.build_alerts(cands)
        self.notify(alerts)
        warnings = self.health()
        if warnings:
            self.discord.send(content="⚠️ **flight-bot 異常**\n" + "\n".join(warnings))
        return self.report
