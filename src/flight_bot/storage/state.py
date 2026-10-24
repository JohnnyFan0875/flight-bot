"""state.sqlite：配對用的最新價格、明細快取、歷史價格、通知記錄、執行記錄。"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    mode TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL DEFAULT '',  -- 單程為空字串
    price INTEGER,                          -- NULL = 沒有航班
    source TEXT NOT NULL,                   -- calendar / per_date
    queried_at TEXT NOT NULL,
    PRIMARY KEY (mode, origin, dest, depart_date, return_date)
);
CREATE TABLE IF NOT EXISTS details (
    mode TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL DEFAULT '',
    price INTEGER,
    price_level TEXT,
    flight_json TEXT,
    queried_at TEXT NOT NULL,
    PRIMARY KEY (mode, origin, dest, depart_date, return_date)
);
CREATE TABLE IF NOT EXISTS combo_history (
    run_date TEXT NOT NULL,
    dest TEXT NOT NULL,
    mode TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    origins TEXT NOT NULL,
    price INTEGER NOT NULL,
    PRIMARY KEY (run_date, dest, mode, depart_date)
);
CREATE INDEX IF NOT EXISTS combo_history_month
    ON combo_history (dest, mode, substr(depart_date, 1, 7), run_date);
CREATE TABLE IF NOT EXISTS notified (
    key TEXT PRIMARY KEY,
    price INTEGER NOT NULL,
    notified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_log (
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL DEFAULT '',
    calendar_price INTEGER,
    detail_price INTEGER,
    logged_at TEXT NOT NULL
);
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


def _ret(d: date | None) -> str:
    return d.isoformat() if d else ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    # ----- quotes -----

    def upsert_quotes(self, rows: list[tuple]) -> None:
        """rows：(mode, origin, dest, depart_date, return_date|None, price|None, source)。"""
        ts = now_iso()
        self.db.executemany(
            "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(m, o, d, dep.isoformat(), _ret(r), p, s, ts) for m, o, d, dep, r, p, s in rows],
        )

    def load_quotes(self, mode: str, origin: str, dest: str, fresh_since: datetime) -> dict:
        """回傳 {(depart_date, return_date|None): price}，只含 fresh_since 之後更新、且有價格的日期。"""
        cur = self.db.execute(
            "SELECT depart_date, return_date, price FROM quotes "
            "WHERE mode = ? AND origin = ? AND dest = ? AND price IS NOT NULL AND queried_at >= ?",
            (mode, origin, dest, fresh_since.isoformat(timespec="seconds")),
        )
        return {
            (date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"]) if r["return_date"] else None): r["price"]
            for r in cur
        }

    # ----- details -----

    def upsert_detail(self, mode, origin, dest, depart: date, ret: date | None, price, price_level, flight: dict | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO details VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, origin, dest, depart.isoformat(), _ret(ret), price, price_level,
             json.dumps(flight, ensure_ascii=False) if flight else None, now_iso()),
        )

    def get_detail(self, mode, origin, dest, depart: date, ret: date | None, since: datetime) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM details WHERE mode = ? AND origin = ? AND dest = ? AND depart_date = ? "
            "AND return_date = ? AND queried_at >= ?",
            (mode, origin, dest, depart.isoformat(), _ret(ret), since.isoformat(timespec="seconds")),
        ).fetchone()

    # ----- combo_history -----

    def add_history(self, run_date: date, rows: list[tuple]) -> None:
        """rows：(dest, mode, depart_date, return_date, origins, price)，每個出發日只存最便宜的一組。"""
        self.db.executemany(
            "INSERT OR REPLACE INTO combo_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_date.isoformat(), dest, mode, dep.isoformat(), ret.isoformat(), origins, price)
             for dest, mode, dep, ret, origins, price in rows],
        )
        cutoff = (run_date - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
        self.db.execute("DELETE FROM combo_history WHERE run_date < ?", (cutoff,))

    def history_start(self, dest: str, mode: str) -> date | None:
        row = self.db.execute(
            "SELECT MIN(run_date) FROM combo_history WHERE dest = ? AND mode = ?", (dest, mode)
        ).fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def month_baseline(self, dest: str, mode: str, month: str, before: date, median_days: int = 30) -> tuple[int | None, float | None]:
        """(歷史最低價, 近 median_days 天的中位數)，只看 before 之前的執行。month 格式 YYYY-MM。"""
        lowest = self.db.execute(
            "SELECT MIN(price) FROM combo_history WHERE dest = ? AND mode = ? "
            "AND substr(depart_date, 1, 7) = ? AND run_date < ?",
            (dest, mode, month, before.isoformat()),
        ).fetchone()[0]
        since = (before - timedelta(days=median_days)).isoformat()
        prices = [
            r[0]
            for r in self.db.execute(
                "SELECT price FROM combo_history WHERE dest = ? AND mode = ? "
                "AND substr(depart_date, 1, 7) = ? AND run_date >= ? AND run_date < ?",
                (dest, mode, month, since, before.isoformat()),
            )
        ]
        return lowest, (statistics.median(prices) if prices else None)

    def history_rows(self, since: date) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM combo_history WHERE run_date >= ? ORDER BY run_date", (since.isoformat(),)
        ).fetchall()

    # ----- notified -----

    def last_notified(self, key: str) -> int | None:
        row = self.db.execute("SELECT price FROM notified WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def mark_notified(self, key: str, price: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO notified VALUES (?, ?, ?)", (key, price, now_iso()))

    # ----- verify_log -----

    def log_verify(self, run_id, mode, origin, dest, depart: date, ret: date | None, calendar_price, detail_price) -> None:
        self.db.execute(
            "INSERT INTO verify_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, mode, origin, dest, depart.isoformat(), _ret(ret), calendar_price, detail_price, now_iso()),
        )

    def verify_rows(self, since: datetime) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM verify_log WHERE logged_at >= ?", (since.isoformat(timespec="seconds"),)
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
