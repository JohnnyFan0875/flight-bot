from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol

PriceLevel = Literal["low", "typical", "high"]


class ProviderError(Exception):
    """查詢失敗（網路錯誤、被擋、回應格式改變）。"""


@dataclass
class CalendarQuote:
    depart_date: date
    return_date: date | None
    price: int | None  # TWD；None = 沒有航班


@dataclass
class Segment:
    airline: str
    flight_no: str
    from_airport: str
    to_airport: str
    depart_at: datetime  # 當地時間
    arrive_at: datetime
    duration_min: int


@dataclass
class Flight:
    airlines: list[str]
    segments: list[Segment]
    stops: int
    duration_min: int
    layovers_min: list[int]
    price: int  # TWD

    @property
    def depart_at(self) -> datetime:
        return self.segments[0].depart_at

    @property
    def arrive_at(self) -> datetime:
        return self.segments[-1].arrive_at

    @property
    def flight_nos(self) -> list[str]:
        return [f"{s.airline}{s.flight_no}" for s in self.segments]

    def to_dict(self) -> dict:
        d = asdict(self)
        for s in d["segments"]:
            s["depart_at"] = s["depart_at"].isoformat()
            s["arrive_at"] = s["arrive_at"].isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Flight:
        segments = [
            Segment(
                **{
                    **s,
                    "depart_at": datetime.fromisoformat(s["depart_at"]),
                    "arrive_at": datetime.fromisoformat(s["arrive_at"]),
                }
            )
            for s in d["segments"]
        ]
        return cls(**{**d, "segments": segments})


@dataclass
class SearchResult:
    flights: list[Flight] = field(default_factory=list)
    price_level: PriceLevel | None = None
    typical_range: tuple[int, int] | None = None
    level_code: int | None = None  # Google 原始的等級代碼，留作分析


class Provider(Protocol):
    def calendar(
        self,
        origin: str,
        dest: str,
        start: date,
        end: date,
        max_stops: int,
        trip_length: int | None = None,
    ) -> list[CalendarQuote]: ...

    def search(
        self,
        origin: str,
        dest: str,
        depart: date,
        ret: date | None,
        max_stops: int,
    ) -> SearchResult: ...
