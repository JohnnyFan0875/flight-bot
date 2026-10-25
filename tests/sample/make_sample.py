"""用模擬資料產生範例報告（不打 Google），結果存成 tests/sample/report.html。

報告格式（report.py、report_template.html）改了之後執行這個腳本更新範例：

    uv run python tests/sample/make_sample.py

tests/test_report_sample.py 會確認產生的內容和存下來的範例一致。
所有時間和價格都是固定的，同樣的程式每次產生的 HTML 都一樣。
"""

from __future__ import annotations

import contextlib
import io
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flight_bot.config import Config
from flight_bot.daily import Daily
from flight_bot.notify.discord import Discord
from flight_bot.providers.base import Flight, SearchResult, Segment
from flight_bot.report import build_data, render
from flight_bot.runner import Runner
from flight_bot.storage.raw import RawWriter
from flight_bot.storage.state import State

OUTPUT = Path(__file__).with_name("report.html")
TODAY = date(2026, 10, 25)
STARTED = datetime(2026, 10, 24, 19, 0, 2, tzinfo=timezone.utc)  # 台灣時間 03:00
FINISHED = STARTED + timedelta(minutes=19, seconds=24)
RUN_ID = "20261024T190002Z-a1b2c3"

# 範例用的固定設定，不讀 config.yaml，避免改目的地時範例跟著變
ROUTE = {"query_start": "2026-10-01", "query_end": "2027-02-28", "outbound_time": "06:00-14:00",
         "return_time": "14:00-21:00", "trip_days": [5, 6, 7], "max_stops": 0}
CONFIG = {
    "runner": {"delay_seconds": [0, 0], "retries": 0},
    "report": {"url": "https://johnnyfan0875.github.io/flight-bot/"},
    "destinations": [
        {"name": "倫敦範例", "enabled": False, "origins": ["TPE"], "dests": ["LHR"], **ROUTE, "max_stops": 1},
        {"name": "北海道", "origins": ["TPE"], "dests": ["CTS"], **ROUTE},
        {"name": "仙台", "origins": ["TPE"], "dests": ["SDJ"], **ROUTE, "trip_days": [4, 5]},
        {"name": "東京", "origins": ["TPE"], "dests": ["NRT", "HND"], **ROUTE},
        {"name": "東京羽田（含松山）", "origins": ["TPE", "TSA"], "dests": ["HND"], **ROUTE},
    ],
}

BASE = {"CTS": 6200, "SDJ": 5600, "NRT": 4300, "HND": 4800}
# (航空公司, 班號, 出發時, 出發分, 飛行分鐘)；有幾班刻意落在設定的時段外，會被過濾掉
OUT_FLIGHTS = {
    "CTS": [("IT", "230", 7, 15, 250), ("BR", "116", 9, 0, 245), ("CI", "130", 8, 50, 240), ("JX", "850", 15, 30, 245)],
    "SDJ": [("IT", "254", 7, 35, 190), ("BR", "118", 10, 20, 195)],
    "NRT": [("IT", "200", 6, 50, 185), ("TR", "874", 11, 35, 185), ("JX", "800", 8, 30, 180), ("MM", "620", 13, 10, 190),
            ("IT", "202", 17, 40, 185)],
    "HND": [("BR", "192", 7, 30, 190), ("IT", "216", 6, 50, 195), ("CI", "220", 8, 0, 190)],
}
BACK_FLIGHTS = {
    "CTS": [("IT", "231", 14, 30, 295), ("BR", "115", 16, 0, 290), ("CI", "131", 10, 40, 290)],
    "SDJ": [("IT", "255", 19, 40, 260), ("BR", "117", 12, 10, 255)],
    "NRT": [("IT", "201", 14, 20, 240), ("TR", "875", 17, 10, 235), ("JX", "801", 15, 45, 240), ("MM", "627", 21, 50, 235)],
    "HND": [("BR", "191", 15, 10, 240), ("CI", "223", 18, 5, 230), ("IT", "217", 9, 50, 235)],
}
# 松山只有飛羽田的這幾班
TSA_FLIGHTS = {"out": [("BR", "190", 7, 50, 185), ("CI", "222", 9, 55, 185)], "back": [("BR", "189", 16, 35, 235), ("CI", "221", 19, 25, 230)]}


def _season(d: date) -> float:
    return 1.35 if d.month in (12, 1, 2) else 1.0


class FakeProvider:
    """價格由日期決定，再依執行日小幅漲跌，模擬降價和漲價。"""

    def __init__(self, run_day: date):
        self.run_day = run_day

    def calendar(self, *args, **kwargs):
        raise NotImplementedError

    def search(self, origin, dest, depart, ret, max_stops):
        outbound = dest in BASE
        place = dest if outbound else origin
        rnd = random.Random(f"{origin}{dest}{depart}")
        drift = random.Random(f"{origin}{dest}{depart}{self.run_day}").choice([0, 0, 0, -200, -300, 150, 250])
        if "TSA" in (origin, dest):
            schedule = TSA_FLIGHTS["out" if outbound else "back"]
        else:
            schedule = (OUT_FLIGHTS if outbound else BACK_FLIGHTS)[place]
        flights = []
        for k, (code, no, hh, mm, dur) in enumerate(schedule):
            # 偶爾有一班前幾天還沒開賣（第一班一定有），示範「新出現」
            if k and self.run_day < TODAY and random.Random(f"{origin}{dest}{depart}{no}{self.run_day}").random() < 0.08:
                continue
            price = int(BASE[place] * _season(depart) * rnd.uniform(0.62, 1.25) / 10) * 10 + drift
            dep = datetime(depart.year, depart.month, depart.day, hh, mm)
            arr = dep + timedelta(minutes=dur + (60 if outbound else -60))  # 台日時差 1 小時
            flights.append(Flight([code], [Segment(code, no, origin, dest, dep, arr, dur)], 0, dur, [], price))
        typical = (int(BASE[place] * _season(depart) * 0.66), int(BASE[place] * _season(depart) * 1.3))
        low = min(f.price for f in flights)
        level = "low" if low < typical[0] else "high" if low > typical[1] else "typical"
        return SearchResult(flights, level, typical)


def _seed_history(state: State, cfg: Config) -> None:
    """過去 11 天的歷史單程價格，比模擬的現價略高，讓一部分航班符合條件。"""
    for n in range(1, 12):
        run = TODAY - timedelta(days=n)
        rows = []
        for r in cfg.routes():
            for origin, dest, outbound in r.legs():
                place = dest if outbound else origin
                start, end = r.date_range(outbound, run)
                for k in range((end - start).days + 1):
                    d = start + timedelta(days=k)
                    price = int(BASE[place] * _season(d) * random.Random(f"{r.id}{origin}{dest}{d}{n}").uniform(0.8, 1.0))
                    rows.append((r.id, origin, dest, d, price))
        state.add_history(run, rows)


def _run(cfg: Config, state: State, day: date, run_id: str) -> Daily:
    daily = Daily(cfg, FakeProvider(day), state, RawWriter(run_id, day), Discord(dry_run=True),
                  Runner(cfg.runner, sleep=lambda s: None), day, run_id, seed=1)
    with contextlib.redirect_stdout(io.StringIO()):
        daily.run()
    return daily


def build_sample_html() -> str:
    cfg = Config.model_validate(CONFIG)
    state = State()
    _seed_history(state, cfg)
    for back in (2, 1):  # 前兩天也跑過，讓狀態有新上榜、降價、漲價、持平
        _run(cfg, state, TODAY - timedelta(days=back), f"prev{back}")
    daily = _run(cfg, state, TODAY, RUN_ID)

    data = build_data(cfg, daily.report, daily.runner.stats, RUN_ID, TODAY, STARTED, FINISHED,
                      daily.coverage_note(), [(TODAY - timedelta(days=i)).isoformat() for i in (2, 1, 0)])
    return render(data)


if __name__ == "__main__":
    html = build_sample_html()
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"已更新 {OUTPUT}（{len(html) // 1024} KB）")
