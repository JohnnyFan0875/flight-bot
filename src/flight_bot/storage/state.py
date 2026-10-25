"""state.sqlite：各日期最近一次查到的航班、單程歷史價格、執行記錄。"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    max_stops INTEGER NOT NULL,
    price_level TEXT,                       -- Google 價格等級
    typical_low INTEGER,                    -- Google 的典型價格區間
    typical_high INTEGER,
    flights_json TEXT NOT NULL,             -- 這一天查到的全部航班（[] = 沒有航班）
    run_date TEXT NOT NULL,                 -- 哪一天的執行查到的
    queried_at TEXT NOT NULL,
    prev_flights_json TEXT,                 -- 前一天以前最後一次查到的航班，算漲跌用
    prev_run_date TEXT,
    PRIMARY KEY (origin, dest, depart_date)
);
CREATE TABLE IF NOT EXISTS leg_history (
    run_date TEXT NOT NULL,
    route TEXT NOT NULL,                    -- 分組/航線名稱
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price INTEGER NOT NULL,                 -- 當天符合航線規則的最低單程價格
    PRIMARY KEY (run_date, route, origin, dest, depart_date)
);
CREATE INDEX IF NOT EXISTS leg_history_month
    ON leg_history (route, origin, dest, substr(depart_date, 1, 7), run_date);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    empty INTEGER NOT NULL DEFAULT 0,
    aborted INTEGER NOT NULL DEFAULT 0,
    calendar_failed INTEGER NOT NULL DEFAULT 0,
    requests_json TEXT
);
"""

HISTORY_KEEP_DAYS = 400

# 舊版 state 缺少的欄位：(資料表, 欄位, 型別)
MIGRATIONS: list[tuple[str, str, str]] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, kind in MIGRATIONS:
            columns = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # ----- searches -----

    def upsert_search(self, origin, dest, depart: date, max_stops: int, price_level, typical: tuple[int, int] | None,
                      flights: list[dict], run_date: date) -> None:
        """同一天重跑時，保留前一天以前的那份當作比較對象。"""
        old = self.get_search(origin, dest, depart)
        day = run_date.isoformat()
        prev_json, prev_day = None, None
        if old is not None:
            if old["run_date"] < day:
                prev_json, prev_day = old["flights_json"], old["run_date"]
            else:
                prev_json, prev_day = old["prev_flights_json"], old["prev_run_date"]
        self.db.execute(
            "INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (origin, dest, depart.isoformat(), max_stops, price_level,
             typical[0] if typical else None, typical[1] if typical else None,
             json.dumps(flights, ensure_ascii=False), day, now_iso(), prev_json, prev_day),
        )

    def get_search(self, origin, dest, depart: date) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM searches WHERE origin = ? AND dest = ? AND depart_date = ?",
            (origin, dest, depart.isoformat()),
        ).fetchone()

    def load_searches(self, origin: str, dest: str, start: date, end: date, fresh_since: datetime) -> list[sqlite3.Row]:
        """start 到 end 之間、fresh_since 之後查過的日期，依日期排序。"""
        return self.db.execute(
            "SELECT * FROM searches WHERE origin = ? AND dest = ? AND depart_date BETWEEN ? AND ? "
            "AND queried_at >= ? ORDER BY depart_date",
            (origin, dest, start.isoformat(), end.isoformat(), fresh_since.isoformat(timespec="seconds")),
        ).fetchall()

    # ----- leg_history -----

    def add_history(self, run_date: date, rows: list[tuple]) -> None:
        """rows：(route, origin, dest, depart_date, price)，每個日期存符合航線規則的最低價。"""
        self.db.executemany(
            "INSERT OR REPLACE INTO leg_history VALUES (?, ?, ?, ?, ?, ?)",
            [(run_date.isoformat(), route, o, d, dep.isoformat(), price) for route, o, d, dep, price in rows],
        )
        cutoff = (run_date - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
        self.db.execute("DELETE FROM leg_history WHERE run_date < ?", (cutoff,))

    def history_start(self, route: str) -> date | None:
        row = self.db.execute("SELECT MIN(run_date) FROM leg_history WHERE route = ?", (route,)).fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def month_baseline(self, route: str, origin: str, dest: str, month: str, before: date,
                       median_days: int = 30) -> tuple[int | None, float | None]:
        """(歷史最低價, 近 median_days 天的中位數)，只看 before 之前的執行。month 格式 YYYY-MM。"""
        where = "route = ? AND origin = ? AND dest = ? AND substr(depart_date, 1, 7) = ?"
        args = (route, origin, dest, month)
        lowest = self.db.execute(
            f"SELECT MIN(price) FROM leg_history WHERE {where} AND run_date < ?", (*args, before.isoformat()),
        ).fetchone()[0]
        since = (before - timedelta(days=median_days)).isoformat()
        prices = [
            r[0]
            for r in self.db.execute(
                f"SELECT price FROM leg_history WHERE {where} AND run_date >= ? AND run_date < ?",
                (*args, since, before.isoformat()),
            )
        ]
        return lowest, (statistics.median(prices) if prices else None)

    def history_rows(self, since: date) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM leg_history WHERE run_date >= ? ORDER BY run_date", (since.isoformat(),)
        ).fetchall()

    # ----- runs -----

    def start_run(self, run_id: str, mode: str) -> None:
        self.db.execute("INSERT INTO runs (run_id, mode, started_at) VALUES (?, ?, ?)", (run_id, mode, now_iso()))

    def finish_run(self, run_id: str, ok: int, failed: int, empty: int, aborted: bool, calendar_failed: int, requests: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, failed = ?, empty = ?, aborted = ?, "
            "calendar_failed = ?, requests_json = ? WHERE run_id = ?",
            (now_iso(), ok, failed, empty, int(aborted), calendar_failed, json.dumps(requests), run_id),
        )

    def recent_runs(self, mode: str, since: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs WHERE mode = ? AND started_at >= ? AND finished_at IS NOT NULL ORDER BY started_at",
            (mode, since.isoformat(timespec="seconds")),
        ).fetchall()

    def last_success(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(started_at) FROM runs WHERE ok > 0").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None
