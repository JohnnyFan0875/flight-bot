from datetime import date, datetime, timedelta

import pytest

from flight_bot.daily import Daily
from flight_bot.notify import weekly
from flight_bot.notify.discord import Discord, batches
from flight_bot.providers.base import CalendarQuote, Flight, ProviderError, SearchResult, Segment
from flight_bot.runner import Runner
from flight_bot.storage.backend import LocalBackend
from flight_bot.storage.raw import RawWriter
from flight_bot.storage.state import State

from .conftest import make_config

TODAY = date(2026, 10, 24)


class FakeProvider:
    """去程 3000、回程 3500；cheap_day 那天去程特價。"""

    def __init__(self, cheap_day: date | None = None, calendar_fails: bool = False, level: str = "typical"):
        self.cheap_day = cheap_day
        self.calendar_fails = calendar_fails
        self.level = level
        self.calls = {"calendar": 0, "search": 0}

    def _price(self, origin, d):
        if origin == "TPE":
            return 1500 if d == self.cheap_day else 3000
        if origin == "NRT":
            return 3500
        return None  # 松山沒有航班

    def calendar(self, origin, dest, start, end, max_stops, trip_length=None):
        self.calls["calendar"] += 1
        if self.calendar_fails:
            raise ProviderError("壞掉了")
        out = []
        d = start
        while d <= end:
            price = self._price(origin, d) if "TSA" not in (origin, dest) else None
            out.append(CalendarQuote(d, None, price))
            d += timedelta(days=1)
        return out

    def search(self, origin, dest, depart, ret, max_stops):
        self.calls["search"] += 1
        price = self._price(origin, depart) if "TSA" not in (origin, dest) else None
        if price is None:
            return SearchResult()
        seg = Segment("IT", "200", origin, dest, datetime(depart.year, depart.month, depart.day, 7, 10),
                      datetime(depart.year, depart.month, depart.day, 11, 30), 200)
        return SearchResult([Flight(["IT"], [seg], 0, 200, [], price)], self.level, (2000, 5000))


def make_daily(provider, state, discord, today=TODAY, **cfg):
    config = make_config(**cfg)
    return Daily(config, provider, state, RawWriter("r1", today), discord, Runner(config.runner, sleep=lambda s: None),
                 today, "r1", seed=1)


def seed_history(state, price=6500, days=10):
    # 每個出發日都有一筆過去的歷史價格，讓 warmup 結束
    for n in range(1, days + 1):
        run = TODAY - timedelta(days=n)
        rows = []
        for k in range(30, 211):
            d = TODAY + timedelta(days=k)
            rows.append(("NRT", "one_way", d, d + timedelta(days=5), "TPE", price))
        state.add_history(run, rows)


def test_first_run_collects_without_alerting_on_median():
    provider = FakeProvider()
    state = State()
    discord = Discord(dry_run=True)
    report = make_daily(provider, state, discord).run()
    assert provider.calls["calendar"] == 8
    assert report.calendar_failed == 0
    assert report.combos > 0
    assert report.alerts == []  # warmup 期間，價格等級也只是 typical
    assert state.history_start("NRT", "one_way") == TODAY


def test_cheap_day_triggers_alert_with_details():
    cheap = TODAY + timedelta(days=45)
    provider = FakeProvider(cheap_day=cheap)
    state = State()
    seed_history(state)
    discord = Discord(dry_run=True)
    report = make_daily(provider, state, discord).run()

    [alert] = [a for a in report.alerts if a.combo.depart == cheap]
    assert alert.price == 1500 + 3500
    assert "new_low" in alert.evaluation.triggers and "below_median" in alert.evaluation.triggers
    assert alert.out_flight is not None and alert.out_flight.flight_nos == ["IT200"]
    assert report.detail_tasks <= 40
    embeds = [e for m in discord.sent for e in m["payload"]["embeds"]]
    assert any("TWD 5,000" in e["description"] and "IT200" in e["description"] for e in embeds)

    # 同一天再跑一次：不會重複通知，也不會重複查明細
    discord2 = Discord(dry_run=True)
    before = provider.calls["search"]
    report2 = make_daily(provider, state, discord2).run()
    assert not [a for a in report2.alerts if a.combo.depart == cheap]
    assert provider.calls["search"] - before <= 4  # 只有驗證抽樣


def test_google_low_alert_without_history():
    provider = FakeProvider(level="low")
    state = State()
    discord = Discord(dry_run=True)
    report = make_daily(provider, state, discord, scan={"strategy": "calendar", "detail_budget": 4}).run()
    assert report.alerts
    assert all(a.evaluation.triggers == ["google_low"] for a in report.alerts)
    assert len(report.alerts) == 2  # 預算 4 次 = 2 組單程組合


def test_calendar_failure_falls_back_to_per_date_and_warns():
    provider = FakeProvider(calendar_fails=True)
    state = State()
    discord = Discord(dry_run=True)
    report = make_daily(provider, state, discord, runner={"delay_seconds": [0, 0], "retries": 0,
                                                          "abort_after_consecutive_failures": 100}).run()
    assert report.calendar_failed == 8
    assert report.fallback_tasks > 100
    assert report.combos > 0
    assert any("日期價格 API 全部失敗" in w for w in report.warnings)
    assert any("flight-bot 異常" in (m["payload"].get("content") or "") for m in discord.sent)


def test_calendar_failure_does_not_abort_and_probes_once():
    provider = FakeProvider(calendar_fails=True)
    state = State()
    daily = make_daily(provider, state, Discord(dry_run=True),
                       runner={"delay_seconds": [0, 0], "retries": 0, "abort_after_consecutive_failures": 3})
    report = daily.run()
    assert provider.calls["calendar"] == 1  # 第一次就失敗，剩下的不再打
    assert not daily.runner.stats.aborted
    assert report.fallback_tasks > 100
    assert report.combos > 0


class FlakyCalendar(FakeProvider):
    """日期 API 前 2 次成功，之後全部失敗。"""

    def calendar(self, *args, **kwargs):
        if self.calls["calendar"] >= 2:
            self.calls["calendar"] += 1
            raise ProviderError("壞掉了")
        return super().calendar(*args, **kwargs)


def test_calendar_gives_up_after_three_failures():
    provider = FlakyCalendar()
    daily = make_daily(provider, State(), Discord(dry_run=True),
                       runner={"delay_seconds": [0, 0], "retries": 0, "concurrency": 1})
    report = daily.run()
    assert provider.calls["calendar"] == 5  # 2 次成功 + 3 次失敗後放棄
    assert report.calendar_failed == 6
    assert report.fallback_tasks > 0


class AllBroken(FakeProvider):
    def search(self, *args, **kwargs):
        self.calls["search"] += 1
        raise ProviderError("被擋了")


def test_abort_after_consecutive_failures():
    provider = AllBroken(calendar_fails=True)
    daily = make_daily(provider, State(), Discord(dry_run=True),
                       runner={"delay_seconds": [0, 0], "retries": 0,
                               "abort_after_consecutive_failures": 5, "concurrency": 1})
    report = daily.run()
    assert daily.runner.stats.aborted
    assert provider.calls["search"] == 5
    assert any("提前結束" in w for w in report.warnings)


def test_raw_parquet_written(tmp_path):
    provider = FakeProvider()
    state = State()
    daily = make_daily(provider, state, Discord(dry_run=True))
    daily.run()
    keys = daily.raw.flush(LocalBackend(tmp_path))
    assert any("kind=calendar" in k for k in keys)
    assert any("kind=flights" in k for k in keys)
    import pyarrow.parquet as pq
    table = pq.read_table(tmp_path / keys[0])
    assert table.num_rows > 0


def test_weekly_summary_builds(tmp_path):
    state = State()
    seed_history(state, days=14)
    embeds, files = weekly.build(make_config(), state, TODAY)
    assert set(files) == {"heatmap.png", "trend.png"}
    assert files["heatmap.png"][:4] == b"\x89PNG"
    assert embeds[2]["fields"][0]["name"].startswith("🏆")


def test_discord_batches_respect_limits():
    embeds = [{"title": "x", "description": "a" * 1000} for _ in range(13)]
    groups = batches(embeds)
    assert all(len(g) <= 10 for g in groups)
    assert all(sum(len(e["description"]) + 1 for e in g) <= 6000 for g in groups)
    assert sum(len(g) for g in groups) == 13


@pytest.mark.parametrize("n", [0, 1])
def test_discord_sends_content_once(n):
    d = Discord(dry_run=True)
    d.send(content="hi", embeds=[{"title": "x"}] * (11 * n))
    assert d.sent[0]["payload"].get("content") == "hi"
    assert all("content" not in m["payload"] for m in d.sent[1:])


def test_per_date_strategy_reuses_scan_details():
    provider = FakeProvider(level="low")
    state = State()
    daily = make_daily(provider, state, Discord(dry_run=True), scan={"strategy": "per_date"})
    report = daily.run()
    assert provider.calls["calendar"] == 0
    assert report.calendar_tasks == 0
    assert report.combos > 0
    assert report.alerts  # 掃描時已取得價格等級，google_low 可以觸發
    assert report.verify_tasks == 0
