"""設定檔模型。覆寫優先順序：目的地 > 分組 > defaults。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Mode = Literal["one_way", "round_trip"]

# 可以在 defaults、分組、目的地三個層級設定的欄位
OVERRIDABLE = (
    "origins",
    "search_from_days",
    "search_to_days",
    "depart_weekdays",
    "allow_mixed_origins",
    "max_stops",
    "max_total_duration_h",
    "max_layover_h",
    "min_layover_min",
    "trip_length_days",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_airport(code: str) -> str:
    if len(code) != 3 or not code.isalpha() or not code.isupper():
        raise ValueError(f"機場代碼必須是 3 個大寫字母：{code!r}")
    return code


def _check_increasing(pair: list[int] | None, name: str) -> None:
    if pair is not None and (len(pair) != 2 or pair[0] > pair[1]):
        raise ValueError(f"{name} 必須是 [小, 大]：{pair}")


class RouteSettings(Strict):
    """三個層級共用的可覆寫欄位，None 代表沿用上一層。"""

    origins: list[str] | None = None
    search_from_days: int | None = Field(default=None, ge=0)
    search_to_days: int | None = Field(default=None, ge=1)
    depart_weekdays: list[int] | None = None
    allow_mixed_origins: bool | None = None
    max_stops: int | None = Field(default=None, ge=0, le=2)
    max_total_duration_h: float | None = Field(default=None, gt=0)
    max_layover_h: float | None = Field(default=None, gt=0)
    min_layover_min: int | None = Field(default=None, ge=0)
    trip_length_days: list[int] | None = None

    @field_validator("origins")
    @classmethod
    def _origins(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if not v:
                raise ValueError("origins 不能是空的")
            for code in v:
                _check_airport(code)
        return v

    @field_validator("depart_weekdays")
    @classmethod
    def _weekdays(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(d < 0 or d > 6 for d in v):
            raise ValueError("depart_weekdays 只能是 0 到 6")
        return v

    @model_validator(mode="after")
    def _ranges(self) -> RouteSettings:
        _check_increasing(self.trip_length_days, "trip_length_days")
        if (
            self.search_from_days is not None
            and self.search_to_days is not None
            and self.search_from_days > self.search_to_days
        ):
            raise ValueError("search_from_days 必須小於等於 search_to_days")
        return self


class Defaults(RouteSettings):
    origins: list[str] | None = ["TPE"]
    search_from_days: int | None = 30
    search_to_days: int | None = 210
    depart_weekdays: list[int] | None = []
    allow_mixed_origins: bool | None = True
    max_stops: int | None = 0
    trip_length_days: list[int] | None = [5, 7]


class Destination(RouteSettings):
    code: str
    enabled: bool = True

    _code = field_validator("code")(classmethod(lambda cls, v: _check_airport(v)))


class Group(RouteSettings):
    mode: Mode
    destinations: list[Destination] = []


class Tier(Strict):
    until_days: int = Field(ge=1)
    every: int = Field(ge=1)


class ScanConfig(Strict):
    # calendar 需要 Google 頁面 JavaScript 產生的 x-goog-batchexecute-bgr header，純 HTTP 請求會被擋
    strategy: Literal["calendar", "per_date"] = "per_date"
    calendar_max_days: int = Field(default=180, ge=1, le=180)
    fallback_to_per_date: bool = True
    per_date_tiers: list[Tier] = [
        Tier(until_days=60, every=1),
        Tier(until_days=120, every=3),
        Tier(until_days=217, every=10),
    ]
    detail_budget: int = Field(default=40, ge=0)
    verify_samples: int = Field(default=4, ge=0)

    @field_validator("per_date_tiers")
    @classmethod
    def _tiers(cls, v: list[Tier]) -> list[Tier]:
        if not v:
            raise ValueError("per_date_tiers 不能是空的")
        days = [t.until_days for t in v]
        if days != sorted(days) or len(set(days)) != len(days):
            raise ValueError("per_date_tiers 的 until_days 必須遞增")
        return v

    def max_every(self) -> int:
        return max(t.every for t in self.per_date_tiers)


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
    renotify_drop: int = Field(default=300, ge=0)


class WeeklyConfig(Strict):
    enabled: bool = True
    weekday: int = Field(default=0, ge=0, le=6)
    hour: int = Field(default=9, ge=0, le=23)


class HealthConfig(Strict):
    max_failure_rate: float = Field(default=0.30, ge=0, le=1)
    max_days_without_success: int = Field(default=3, ge=1)


class Config(Strict):
    defaults: Defaults = Defaults()
    scan: ScanConfig = ScanConfig()
    runner: RunnerConfig = RunnerConfig()
    alerts: AlertsConfig = AlertsConfig()
    weekly_summary: WeeklyConfig = WeeklyConfig()
    health: HealthConfig = HealthConfig()
    groups: dict[str, Group] = {}

    @model_validator(mode="after")
    def _tiers_cover_returns(self) -> Config:
        last = self.scan.per_date_tiers[-1].until_days
        for t in self.targets():
            if t.latest_day() > last:
                raise ValueError(
                    f"per_date_tiers 最後一層 until_days={last} 沒有涵蓋 "
                    f"{t.group}/{t.dest} 的最晚日期（第 {t.latest_day()} 天）"
                )
        return self

    def targets(self) -> list[Target]:
        """展開所有啟用中的目的地，套用覆寫規則。"""
        out = []
        for group_name, group in self.groups.items():
            for dest in group.destinations:
                if not dest.enabled:
                    continue
                merged: dict = {}
                for layer in (self.defaults, group, dest):
                    for key in OVERRIDABLE:
                        value = getattr(layer, key)
                        if value is not None:
                            merged[key] = value
                if merged["search_from_days"] > merged["search_to_days"]:
                    raise ValueError(f"{group_name}/{dest.code}：search_from_days 大於 search_to_days")
                out.append(
                    Target(
                        group=group_name,
                        dest=dest.code,
                        mode=group.mode,
                        origins=tuple(merged["origins"]),
                        search_from_days=merged["search_from_days"],
                        search_to_days=merged["search_to_days"],
                        depart_weekdays=tuple(merged["depart_weekdays"]),
                        allow_mixed_origins=merged["allow_mixed_origins"],
                        max_stops=merged["max_stops"],
                        max_total_duration_h=merged.get("max_total_duration_h"),
                        max_layover_h=merged.get("max_layover_h"),
                        min_layover_min=merged.get("min_layover_min"),
                        trip_length_days=(merged["trip_length_days"][0], merged["trip_length_days"][1]),
                    )
                )
        return out


@dataclass(frozen=True)
class Target:
    """套用覆寫後，一個目的地的完整設定。"""

    group: str
    dest: str
    mode: Mode
    origins: tuple[str, ...]
    search_from_days: int
    search_to_days: int
    depart_weekdays: tuple[int, ...]
    allow_mixed_origins: bool
    max_stops: int
    max_total_duration_h: float | None
    max_layover_h: float | None
    min_layover_min: int | None
    trip_length_days: tuple[int, int]

    def latest_day(self) -> int:
        """會查到的最晚日期距今天幾天（單程模式的回程方向最晚）。"""
        if self.mode == "one_way":
            return self.search_to_days + self.trip_length_days[1]
        return self.search_to_days


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config.model_validate(data)
