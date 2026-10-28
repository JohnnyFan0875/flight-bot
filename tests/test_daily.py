from datetime import date, datetime, timedelta

import pytest

from flight_bot.daily import Daily
from flight_bot.notify import weekly
from flight_bot.notify.discord import Discord, batches
from flight_bot.providers.base import Flight, ProviderError, SearchResult, Segment
from flight_bot.runner import Runner
from flight_bot.storage.backend import LocalBackend
from flight_bot.storage.raw import RawWriter
from flight_bot.storage.state import State

from .conftest import make_config, route

TODAY = date(2026, 10, 24)
ROUTE = "東京"


class FakeProvider:
    """去程 07:10 3000、09:30 3400；回程 3500；cheap_day 那天去程特價；松山沒有航班。"""

    def __init__(self, cheap_day: date | None = None, level: str = "typical", typical=(2000, 5000)):
        self.cheap_day = cheap_day
        self.level = level
        self.typical = typical
        self.calls = 0

    @staticmethod
    def _flight(origin, dest, d, hh, mm, price, no="200"):
        seg = Segment("IT", no, origin, dest, datetime(d.year, d.month, d.day, hh, mm),
                      datetime(d.year, d.month, d.day, hh + 4, mm), 240)
        return Flight(["IT"], [seg], 0, 240, [], price)

    def search(self, origin, dest, depart, ret, max_stops):
        self.calls += 1
        if "TSA" in (origin, dest):
            return SearchResult()
        if origin == "TPE":
            cheap = 1500 if depart == self.cheap_day else 3000
            flights = [self._flight(origin, dest, depart, 7, 10, cheap), self._flight(origin, dest, depart, 9, 30, 3400, "202")]
        else:
            flights = [self._flight(origin, dest, depart, 15, 0, 3500, "201")]
        return SearchResult(flights, self.level, self.typical)


def make_daily(provider, state, discord=None, today=TODAY, **cfg):
    config = make_config(**cfg)
    return Daily(config, provider, state, RawWriter("r1", today), discord or Discord(dry_run=True),
                 Runner(config.runner, sleep=lambda s: None), today, "r1", seed=1)


def seed_history(state, price=3000, days=10, route_id=ROUTE):
    # 每個日期都有過去的歷史價格，讓 warmup 結束
    for n in range(1, days + 1):
        rows = []
        for k in range(130):
            d = date(2026, 11, 1) + timedelta(days=k)
            rows += [(route_id, "TPE", "NRT", d, price), (route_id, "NRT", "TPE", d, 3500)]
        state.add_history(TODAY - timedelta(days=n), rows)


def test_first_run_collects_all_flights_without_alerting():
    provider = FakeProvider()
    state = State()
    report = make_daily(provider, state).run()
    out = [leg for leg in report.legs if leg.outbound]
    assert out and all(len(leg.flights) == 2 for leg in out)  # 同一天的航班全部保留
    assert [f.price for f in out[0].flights] == [3000, 3400]
    assert not any(leg.origin == "TSA" or leg.dest == "TSA" for leg in report.legs)  # 沒有航班的日期不列
    assert report.hits == []  # warmup 期間，也沒有低於 Google 典型區間
    assert state.history_start(ROUTE) == TODAY
    assert all(f.status == "new" for leg in report.legs for f in leg.flights)


def test_cheap_day_triggers_on_one_way_price():
    cheap = date(2026, 12, 8)
    state = State()
    seed_history(state)
    discord = Discord(dry_run=True)
    report = make_daily(FakeProvider(cheap_day=cheap), state, discord).run()

    hits = [(leg, f) for leg, f in report.hits]
    assert {leg.day for leg, _ in hits} == {cheap}
    [(leg, f)] = hits
    assert leg.outbound and f.price == 1500
    assert f.evaluation.triggers == ["new_low", "below_median", "google_low"]
    assert leg.flights[1].price == 3400 and not leg.flights[1].evaluation.hit  # 同一天較貴的班次不算
    [summary] = [e for m in discord.sent for e in m["payload"]["embeds"]]
    assert summary["title"].startswith("每日報告") and "符合條件的單程航班 **1** 班" in summary["description"]

    # 隔天價格不變：狀態是持平
    report2 = make_daily(FakeProvider(cheap_day=cheap), state, today=TODAY + timedelta(days=1)).run()
    [(_, f2)] = [(leg, f) for leg, f in report2.hits if leg.day == cheap]
    assert f2.status == "same" and f2.prev_price == 1500


def test_price_drop_shows_status():
    cheap = date(2026, 12, 8)
    state = State()
    make_daily(FakeProvider(), state).run()
    report = make_daily(FakeProvider(cheap_day=cheap), state, today=TODAY + timedelta(days=1)).run()
    leg = next(x for x in report.legs if x.day == cheap and x.outbound)
    assert leg.cheapest.status == "down" and leg.cheapest.prev_price == 3000


def test_google_low_without_history():
    report = make_daily(FakeProvider(typical=(3200, 6000)), State()).run()
    assert report.hits
    assert all(leg.outbound and f.price == 3000 for leg, f in report.hits)
    assert all(f.evaluation.triggers == ["google_low"] for _, f in report.hits)


def test_shared_pair_queried_once_but_listed_per_route():
    cfg = {"destinations": [route(name="a", origins=["TPE"], trip_days=[5]), route(name="b", origins=["TPE"], trip_days=[6])]}
    provider = FakeProvider()
    daily = make_daily(provider, State(), **cfg)
    report = daily.run()
    assert provider.calls == report.scan_tasks
    days = {leg.route.name: {x.day for x in report.legs if x.route.name == leg.route.name and x.outbound} for leg in report.legs}
    assert days["a"] == days["b"]


def test_failed_query_keeps_previous_price_for_two_days():
    state = State()
    make_daily(FakeProvider(), state).run()
    # 之後兩天查詢全部失敗：沿用之前的價格，但 queried_at 太舊就不再列出
    state.db.execute("UPDATE searches SET queried_at = '2000-01-01T00:00:00+00:00'")
    daily = make_daily(AllBroken(), state, today=TODAY + timedelta(days=1),
                       runner={"delay_seconds": [0, 0], "retries": 0, "abort_after_consecutive_failures": 3})
    daily.run()
    assert daily.report.legs == []


class AllBroken(FakeProvider):
    def search(self, *args, **kwargs):
        self.calls += 1
        raise ProviderError("被擋了")


def test_abort_after_consecutive_failures():
    provider = AllBroken()
    daily = make_daily(provider, State(), runner={"delay_seconds": [0, 0], "retries": 0,
                                                  "abort_after_consecutive_failures": 5, "concurrency": 1})
    report = daily.run()
    assert daily.runner.stats.aborted
    assert provider.calls == 5
    assert any("提前結束" in w for w in report.warnings)
    assert any("flight-bot 異常" in (m["payload"].get("content") or "") for m in daily.discord.sent)



def test_nothing_to_query_is_not_a_failure():
    # 日期都已經過了：一次都不查，不算異常
    daily = make_daily(FakeProvider(), State(), destinations=[route(query_start="2026-01-01", query_end="2026-02-01")])
    report = daily.run()
    assert report.scan_tasks == 0 and not report.warnings
    assert not any("異常" in (m["payload"].get("content") or "") for m in daily.discord.sent)

def test_raw_parquet_written(tmp_path):
    daily = make_daily(FakeProvider(), State())
    daily.run()
    keys = daily.raw.flush(LocalBackend(tmp_path))
    assert keys and all("kind=flights" in k for k in keys)
    import pyarrow.parquet as pq
    assert pq.read_table(tmp_path / keys[0]).num_rows > 0


def test_weekly_summary_builds():
    state = State()
    seed_history(state, days=14)
    embeds, files = weekly.build(make_config(), state, TODAY)
    assert set(files) == {"heatmap.png", "trend.png"}
    assert files["heatmap.png"][:4] == b"\x89PNG"
    fields = embeds[2]["fields"]
    assert fields[0]["name"].startswith("本週前 3 名")
    assert {f["name"] for f in fields} >= {"東京 去程", "東京 回程"}


def test_weekly_only_current_routes():
    state = State()
    seed_history(state, days=14)  # 歷史裡只有東京
    cfg = make_config(destinations=[route(name="札幌", dests=["CTS"])])
    _, files = weekly.build(cfg, state, TODAY)
    assert files == {}


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


def test_report_html_renders():
    from flight_bot.report import build_data, render

    cheap = date(2026, 12, 8)
    state = State()
    seed_history(state)
    daily = make_daily(FakeProvider(cheap_day=cheap), state)
    report = daily.run()
    data = build_data(daily.cfg, report, daily.runner.stats, "r1", TODAY, daily.started,
                      daily.started + timedelta(minutes=20), [TODAY.isoformat()])
    assert len(data["legs"]) == len(report.legs)
    leg = next(x for x in data["legs"] if x["date"] == cheap.isoformat() and x["out"])
    assert [f["price"] for f in leg["flights"]] == [1500, 3400]
    assert leg["flights"][0]["nos"] == ["IT200"] and leg["flights"][0]["st"] == "new"
    assert leg["url"].startswith("https://www.google.com/travel/flights")
    assert data["routes"][0]["days"] == [5, 6, 7] and data["routes"][0]["range"] == ["2026-11-01", "2027-01-31"]
    page = render(data)
    assert "__DATA__" not in page and "<title>機票總覽 (" in page
    assert "</script>" not in page.split('<script id="data" type="application/json">')[1].split("</script>")[0]


def test_summary_when_nothing_qualifies():
    from flight_bot.notify.alerts import build_summary

    [embed] = build_summary([], TODAY, "https://example.com/")
    assert "今天沒有符合條件的航班" in embed["description"]
    assert "[開啟完整報告](https://example.com/)" in embed["description"]
    assert embed["url"] == "https://example.com/"
