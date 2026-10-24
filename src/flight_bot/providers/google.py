"""Google Flights 非官方介面。

- 日期價格 API（GetCalendarGraph）：一次取得一段日期內每天的最低價
- 逐日查詢：用 fast-flights 產生 tfs 參數，解析頁面內嵌的 JSON

兩者的回應格式都是逆向工程得來，Google 改版時要更新這裡。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from fast_flights import FlightQuery, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .base import CalendarQuote, Flight, ProviderError, SearchResult, Segment

SEARCH_URL = "https://www.google.com/travel/flights"
CALENDAR_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGraph"
)
LANG = "zh-TW"
CURRENCY = "TWD"


def google_stops(max_stops: int) -> int:
    """Google 的轉機數編碼：0 = 不限、1 = 只要直飛、2 = 最多轉 1 次…"""
    return max_stops + 1


def build_query(origin: str, dest: str, depart: date, ret: date | None, max_stops: int):
    flights = [FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=dest, max_stops=max_stops)]
    if ret is not None:
        flights.append(FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin, max_stops=max_stops))
    return create_query(
        flights=flights,
        trip="round-trip" if ret else "one-way",
        language=LANG,
        currency=CURRENCY,
    )


# ---------- 日期價格 API ----------


def _calendar_segment(origin: str, dest: str, day: date, stops: int) -> list:
    return [[[[origin, 0]]], [[[dest, 0]]], None, stops, None, None, day.isoformat(),
            None, None, None, None, None, None, None, 3]


def build_calendar_request(
    origin: str, dest: str, start: date, end: date, max_stops: int, trip_length: int | None
) -> str:
    stops = google_stops(max_stops)
    segments = [_calendar_segment(origin, dest, start, stops)]
    if trip_length is not None:
        segments.append(_calendar_segment(dest, origin, start + timedelta(days=trip_length), stops))
    trip = 1 if trip_length is not None else 2  # 1 = 來回、2 = 單程
    inner: list = [
        None,
        [None, None, trip, None, [], 1, [1, 0, 0, 0], None, None, None, None, None, None,
         segments, None, None, None, 1],
        [start.isoformat(), end.isoformat()],
    ]
    if trip_length is not None:
        inner += [None, [trip_length, trip_length]]
    compact = (",", ":")
    return json.dumps([None, json.dumps(inner, separators=compact)], separators=compact)


def parse_calendar_response(text: str, start: date, end: date) -> list[CalendarQuote]:
    rows = None
    for line in text.splitlines():
        if not line.startswith('[["wrb.fr"'):
            continue
        for entry in json.loads(line):
            if entry[0] == "wrb.fr" and entry[2]:
                inner = json.loads(entry[2])
                rows = inner[1] if len(inner) > 1 else None
    if not rows:
        # 沒有航班時 Google 仍會回傳每一天（價格是 null），完全沒有資料列代表請求格式或範圍有問題
        raise ProviderError("日期價格 API 沒有回傳任何日期")

    prices: dict[tuple[date, date | None], int | None] = {}
    for row in rows:
        depart = date.fromisoformat(row[0])
        ret = date.fromisoformat(row[1]) if row[1] else None
        price = None
        if row[2] and row[2][0] and len(row[2][0]) > 1:
            price = row[2][0][1]
        prices[(depart, ret)] = price

    return [
        CalendarQuote(depart_date=d, return_date=r, price=p)
        for (d, r), p in sorted(prices.items(), key=lambda kv: kv[0][0])
        if start <= d <= end
    ]


# ---------- 逐日查詢 ----------


def extract_payload(html: str) -> list | None:
    """從頁面取出 ds:1 的 JSON。None 代表 Google 回報查無航班。"""
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise ProviderError("頁面沒有航班資料（可能被擋或出現同意頁面）")
    js = script.text()
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return None
    return json.loads(data)


def _time(value: list | None) -> tuple[int, int]:
    # Google 會省略 0：[8] = 08:00、[None, 31] = 00:31
    padded = [*(value or []), None, None]
    return padded[0] or 0, padded[1] or 0


def _dt(d: list[int], t: list | None) -> datetime:
    h, m = _time(t)
    return datetime(d[0], d[1], d[2], h, m)


def _parse_flight(item: list) -> Flight | None:
    info = item[0]
    try:
        price = item[1][0][1]
    except (IndexError, TypeError):
        return None
    if price is None:
        return None

    segments = []
    for s in info[2]:
        code = s[22] or [None, None]
        segments.append(
            Segment(
                airline=code[0] or "",
                flight_no=str(code[1] or ""),
                from_airport=s[3],
                to_airport=s[6],
                depart_at=_dt(s[20], s[8]),
                arrive_at=_dt(s[21], s[10]),
                duration_min=s[11] or 0,
            )
        )
    layovers = [lay[0] for lay in (info[13] or []) if lay and lay[0] is not None]
    airlines = list(dict.fromkeys(s.airline for s in segments if s.airline))
    return Flight(
        airlines=airlines,
        segments=segments,
        stops=len(segments) - 1,
        duration_min=info[9] or sum(s.duration_min for s in segments) + sum(layovers),
        layovers_min=layovers,
        price=price,
    )


def parse_search_payload(payload: list | None) -> SearchResult:
    if payload is None:
        return SearchResult()

    flights: list[Flight] = []
    seen = set()
    # [2] 是「最佳航班」、[3] 是其他航班
    for idx in (2, 3):
        block = payload[idx] if len(payload) > idx else None
        if not block or not block[0]:
            continue
        for item in block[0]:
            f = _parse_flight(item)
            if f is None:
                continue
            key = (tuple(f.flight_nos), f.depart_at, f.price)
            if key not in seen:
                seen.add(key)
                flights.append(f)

    result = SearchResult(flights=flights)
    insight = payload[5] if len(payload) > 5 else None
    if insight:
        try:
            current, low, high = insight[1][1], insight[4][1], insight[5][1]
        except (IndexError, TypeError):
            current = low = high = None
        result.level_code = insight[0] if isinstance(insight[0], int) else None
        if None not in (current, low, high):
            result.typical_range = (low, high)
            # Google 的等級代碼不容易直接對應，改用它附的典型價格區間判斷
            result.price_level = "low" if current < low else "high" if current > high else "typical"
    return result


class GoogleFlightsProvider:
    def __init__(self, proxy: str | None = None, timeout: float = 30):
        self._client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            proxy=proxy,
            cookie_store=True,
            timeout=timeout,
        )

    def calendar(self, origin, dest, start, end, max_stops, trip_length=None):
        body = build_calendar_request(origin, dest, start, end, max_stops, trip_length)
        try:
            res = self._client.post(
                CALENDAR_URL,
                params={"hl": LANG, "curr": CURRENCY, "gl": "TW", "soc-app": "162",
                        "soc-platform": "1", "soc-device": "1", "rt": "c"},
                data={"f.req": body},
            )
        except Exception as e:  # primp 的例外沒有共同基底類別
            raise ProviderError(f"日期價格 API 連線失敗：{e}") from e
        if res.status_code != 200:
            raise ProviderError(f"日期價格 API HTTP {res.status_code}")
        try:
            return parse_calendar_response(res.text, start, end)
        except (ValueError, IndexError, TypeError) as e:
            raise ProviderError(f"日期價格 API 回應格式不符：{e}") from e

    def search(self, origin, dest, depart, ret, max_stops):
        query = build_query(origin, dest, depart, ret, max_stops)
        try:
            res = self._client.get(SEARCH_URL, params=query.params())
        except Exception as e:
            raise ProviderError(f"逐日查詢連線失敗：{e}") from e
        if res.status_code != 200:
            raise ProviderError(f"逐日查詢 HTTP {res.status_code}")
        try:
            return parse_search_payload(extract_payload(res.text))
        except ProviderError:
            raise
        except (ValueError, IndexError, TypeError) as e:
            raise ProviderError(f"逐日查詢回應格式不符：{e}") from e
