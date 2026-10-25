"""每次執行的原始資料，寫成 Parquet（只新增，不覆寫）。"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from ..providers.base import Flight, SearchResult
from .backend import Backend

FLIGHTS_SCHEMA = pa.schema([
    ("run_id", pa.string()),
    ("queried_at", pa.timestamp("s", tz="UTC")),
    ("purpose", pa.string()),
    ("mode", pa.string()),
    ("origin", pa.string()),
    ("dest", pa.string()),
    ("depart_date", pa.date32()),
    ("return_date", pa.date32()),
    ("airlines", pa.list_(pa.string())),
    ("flight_nos", pa.list_(pa.string())),
    ("depart_at", pa.string()),
    ("arrive_at", pa.string()),
    ("stops", pa.int8()),
    ("duration_min", pa.int32()),
    ("layovers_min", pa.list_(pa.int32())),
    ("price", pa.int32()),
    ("currency", pa.string()),
    ("price_level", pa.string()),
    ("status", pa.string()),
    ("error", pa.string()),
])


class RawWriter:
    def __init__(self, run_id: str, run_date: date):
        self.run_id = run_id
        self.run_date = run_date
        self.flights: list[dict] = []

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)

    def add_search(self, purpose, origin, dest, depart: date, result: SearchResult | None,
                   error: str | None = None) -> None:
        base = {
            "run_id": self.run_id, "queried_at": self._now(), "purpose": purpose, "mode": "one_way",
            "origin": origin, "dest": dest, "depart_date": depart, "return_date": None, "currency": "TWD",
        }
        if error is not None or result is None:
            self.flights.append({**base, "status": "error", "error": error})
            return
        if not result.flights:
            self.flights.append({**base, "status": "empty", "price_level": result.price_level})
            return
        for f in result.flights:
            self.flights.append({**base, **_flight_columns(f), "price_level": result.price_level, "status": "ok"})

    def flush(self, backend: Backend) -> list[str]:
        keys = []
        for kind, rows, schema in (("flights", self.flights, FLIGHTS_SCHEMA),):
            if not rows:
                continue
            table = pa.Table.from_pylist(rows, schema=schema)
            buf = io.BytesIO()
            pq.write_table(table, buf, compression="zstd")
            key = f"raw/kind={kind}/date={self.run_date.isoformat()}/part-{self.run_id}.parquet"
            backend.put(key, buf.getvalue())
            keys.append(key)
        return keys


def _flight_columns(f: Flight) -> dict:
    return {
        "airlines": f.airlines,
        "flight_nos": f.flight_nos,
        "depart_at": f.depart_at.isoformat(),
        "arrive_at": f.arrive_at.isoformat(),
        "stops": f.stops,
        "duration_min": f.duration_min,
        "layovers_min": f.layovers_min,
        "price": f.price,
    }
