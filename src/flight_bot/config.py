"""設定檔模型：destinations 是一條條航線，每條航線的設定都寫在自己底下。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import airports

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_airport(code: str) -> str:
    if len(code) != 3 or not code.isalpha() or not code.isupper():
        raise ValueError(f"機場代碼必須是 3 個大寫字母：{code!r}")
    return code


def _airports(v: list[str], name: str) -> list[str]:
    if not v:
        raise ValueError(f"{name} 不能是空的")
    for code in v:
        _check_airport(code)
    if len(set(v)) != len(v):
        raise ValueError(f"{name} 有重複的機場：{v}")
    return v


class RouteConfig(Strict):
    """一條航線：出發地（可多個）往返目的地（可多個）。"""

    name: str | None = None  # 顯示名稱；沒寫就用目的地名稱
    enabled: bool = True
    origins: list[str]
    dests: list[str]
    query_start: date  # 去程出發日的範圍（含兩端），範圍內每天都查
    query_end: date
    trip_days: list[int]  # 網頁配對去回程時使用的天數，例如 [5, 6, 7]
    max_stops: int = Field(default=0, ge=0, le=2)  # 最多轉機次數，0 = 只要直飛
    max_total_duration_h: float | None = Field(default=None, gt=0)  # 單程總飛行時間上限（含轉機）
    max_layover_h: float | None = Field(default=None, gt=0)  # 每次轉機最長等待時間
    min_layover_min: int | None = Field(default=None, ge=0)  # 每次轉機最短等待時間

    @field_validator("origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        return _airports(v, "origins")

    @field_validator("dests")
    @classmethod
    def _dests(cls, v: list[str]) -> list[str]:
        return _airports(v, "dests")

    @field_validator("trip_days")
    @classmethod
    def _trip_days(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("trip_days 不能是空的")
        if any(n < 1 for n in v):
            raise ValueError(f"trip_days 必須都大於 0：{v}")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check(self) -> RouteConfig:
        both = set(self.origins) & set(self.dests)
        if both:
            raise ValueError(f"機場不能同時是出發地和目的地：{sorted(both)}")
        if self.query_start > self.query_end:
            raise ValueError(f"query_start {self.query_start} 晚於 query_end {self.query_end}")
        return self


class RunnerConfig(Strict):
    concurrency: int = Field(default=2, ge=1, le=8)
    delay_seconds: list[float] = [2, 5]
    retries: int = Field(default=2, ge=0)
    abort_after_consecutive_failures: int = Field(default=20, ge=1)

    @field_validator("delay_seconds")
    @classmethod
    def _delay(cls, v: list[float]) -> list[float]:
        if len(v) != 2 or v[0] > v[1] or v[0] < 0:
            raise ValueError("delay_seconds 必須是 [小, 大]")
        return v


class MaxPrice(Strict):
    """單程價格上限。"""

    default: int | None = None
    by_month: dict[int, int] = {}

    def for_month(self, month: int) -> int | None:
        return self.by_month.get(month, self.default)


class AlertsConfig(Strict):
    max_price: MaxPrice | None = None
    use_google_price_level: bool = True
    below_median_pct: float = Field(default=15, ge=0, lt=100)
    notify_new_low: bool = True
    warmup_days: int = Field(default=7, ge=0)


class ReportConfig(Strict):
    url: str | None = None  # 報告網址（GitHub Pages），Discord 總結會附上
    keep_days: int = Field(default=30, ge=1)  # 網站上保留幾天的歷史報告


class WeeklyConfig(Strict):
    enabled: bool = True
    weekday: int = Field(default=0, ge=0, le=6)
    hour: int = Field(default=9, ge=0, le=23)


class HealthConfig(Strict):
    max_failure_rate: float = Field(default=0.30, ge=0, le=1)
    max_days_without_success: int = Field(default=3, ge=1)


class Config(Strict):
    runner: RunnerConfig = RunnerConfig()
    alerts: AlertsConfig = AlertsConfig()
    weekly_summary: WeeklyConfig = WeeklyConfig()
    health: HealthConfig = HealthConfig()
    report: ReportConfig = ReportConfig()
    destinations: list[RouteConfig] = []

    @model_validator(mode="after")
    def _unique_names(self) -> Config:
        names = [r.name for r in self.routes()]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"航線名稱重複：{dup}")
        return self

    def routes(self) -> list[Route]:
        """所有 enabled 的航線。"""
        return [
            Route(
                name=rc.name or "、".join(airports.name(d) for d in rc.dests),
                origins=tuple(rc.origins),
                dests=tuple(rc.dests),
                trip_days=tuple(rc.trip_days),
                query_start=rc.query_start,
                query_end=rc.query_end,
                max_stops=rc.max_stops,
                max_total_duration_h=rc.max_total_duration_h,
                max_layover_h=rc.max_layover_h,
                min_layover_min=rc.min_layover_min,
            )
            for rc in self.destinations
            if rc.enabled
        ]


@dataclass(frozen=True)
class Route:
    """一條航線的完整設定。"""

    name: str
    origins: tuple[str, ...]
    dests: tuple[str, ...]
    trip_days: tuple[int, ...]
    query_start: date
    query_end: date
    max_stops: int = 0
    max_total_duration_h: float | None = None
    max_layover_h: float | None = None
    min_layover_min: int | None = None

    @property
    def id(self) -> str:
        """歷史價格以這個為單位；改名會重新累積。"""
        return self.name

    def legs(self) -> list[tuple[str, str, bool]]:
        """(出發機場, 抵達機場, 是否為去程)：每個出發地 × 每個目的地的去程和回程。"""
        out = [(o, d, True) for o in self.origins for d in self.dests]
        return out + [(d, o, False) for d in self.dests for o in self.origins]

    def date_range(self, outbound: bool, today: date) -> tuple[date, date]:
        """要查的日期（含兩端）：去程是 query_start 到 query_end，回程再加上最短到最長的天數。
        今天以前的日期不查，所以 start 可能晚於 end（這時沒有要查的日期）。"""
        start, end = self.query_start, self.query_end
        if not outbound:
            start, end = start + timedelta(days=self.trip_days[0]), end + timedelta(days=self.trip_days[-1])
        return max(start, today), end


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config.model_validate(data)
