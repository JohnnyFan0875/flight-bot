import json
from datetime import date

import pytest

from flight_bot.providers.base import ProviderError
from flight_bot.providers.google import (
    build_calendar_request,
    extract_payload,
    google_stops,
    parse_calendar_response,
    parse_search_payload,
)


def test_google_stops_encoding():
    assert google_stops(0) == 1  # 只要直飛
    assert google_stops(1) == 2


def test_calendar_request_shape():
    body = json.loads(build_calendar_request("TPE", "NRT", date(2026, 11, 25), date(2027, 1, 24), 0, None))
    inner = json.loads(body[1])
    assert inner[1][2] == 2  # 單程
    assert inner[1][13][0][3] == 1  # 直飛
    assert inner[2] == ["2026-11-25", "2027-01-24"]

    body = json.loads(build_calendar_request("TPE", "NRT", date(2026, 11, 25), date(2027, 1, 24), 1, 5))
    inner = json.loads(body[1])
    assert inner[1][2] == 1  # 來回
    assert inner[1][13][1][6] == "2026-11-30"
    assert inner[4] == [5, 5]


def test_parse_calendar_one_way(fixtures):
    text = (fixtures / "calendar_tpe_nrt_oneway.txt").read_text()
    quotes = parse_calendar_response(text, date(2026, 11, 25), date(2027, 1, 24))
    assert len(quotes) == 61
    assert quotes[0].depart_date == date(2026, 11, 25)
    assert quotes[0].return_date is None
    by_date = {q.depart_date: q.price for q in quotes}
    assert by_date[date(2026, 12, 10)] == 4868  # 和逐日查詢的最低價一致


def test_parse_calendar_round_trip(fixtures):
    text = (fixtures / "calendar_tpe_nrt_rt.txt").read_text()
    quotes = parse_calendar_response(text, date(2026, 11, 25), date(2026, 12, 24))
    assert quotes
    assert all((q.return_date - q.depart_date).days == 5 for q in quotes)
    by_date = {q.depart_date: q.price for q in quotes}
    assert by_date[date(2026, 12, 10)] == 8505


def test_parse_calendar_empty_is_error():
    with pytest.raises(ProviderError):
        parse_calendar_response(")]}'\n\n12\n[[\"wrb.fr\",null,\"[null,null]\"]]", date(2027, 1, 1), date(2027, 1, 2))


def test_parse_search_html_direct(fixtures):
    payload = extract_payload((fixtures / "search_tpe_nrt_direct.html").read_text())
    result = parse_search_payload(payload)
    assert len(result.flights) == 20  # 包含「最佳航班」區塊
    assert min(f.price for f in result.flights) == 4868
    tr866 = next(f for f in result.flights if f.flight_nos == ["TR866"])
    assert tr866.price == 6451
    assert tr866.stops == 0
    assert tr866.depart_at.isoformat() == "2026-12-10T06:50:00"
    assert tr866.arrive_at.isoformat() == "2026-12-10T10:55:00"
    assert tr866.duration_min == 185
    assert result.typical_range == (3650, 5800)
    assert result.price_level == "typical"


def test_parse_search_layovers(fixtures):
    payload = json.loads((fixtures / "search_tpe_cts_1stop.json").read_text())
    result = parse_search_payload(payload)
    one_stop = [f for f in result.flights if f.stops == 1]
    assert one_stop
    f = next(f for f in one_stop if f.segments[0].to_airport == "MNL")
    assert f.layovers_min == [290]
    assert f.duration_min == 755
    assert result.typical_range == (6500, 10500)
    assert result.price_level == "typical"


def test_parse_search_round_trip(fixtures):
    payload = json.loads((fixtures / "search_tpe_nrt_rt.json").read_text())
    result = parse_search_payload(payload)
    assert min(f.price for f in result.flights) == 8505
    assert result.price_level == "typical"


def test_blocked_page_is_error():
    with pytest.raises(ProviderError):
        extract_payload("<html><body>Before you continue to Google</body></html>")
