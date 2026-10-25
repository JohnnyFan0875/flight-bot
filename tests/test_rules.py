from datetime import datetime, timedelta

from flight_bot import rules
from flight_bot.providers.base import Flight, Segment

from .conftest import make_config, route


def flight(hh: int, mm: int = 0, price: int = 3000, stops: int = 0) -> Flight:
    dep = datetime(2027, 3, 14, hh, mm)
    seg = Segment("IT", "200", "TPE", "NRT", dep, dep + timedelta(hours=3), 180)
    return Flight(["IT"], [seg] * (stops + 1), stops, 180, [90] * stops, price)


def target(**kw):
    return make_config(destinations=[route(**kw)]).routes()[0]


def test_outbound_and_return_windows():
    r = target(outbound_time="06:00-14:00", return_time="14:00-21:00")
    assert rules.passes(flight(6, 0), r, outbound=True)
    assert rules.passes(flight(14, 0), r, outbound=True)
    assert not rules.passes(flight(14, 5), r, outbound=True)
    assert not rules.passes(flight(5, 59), r, outbound=True)
    assert rules.passes(flight(19, 40), r, outbound=False)
    assert not rules.passes(flight(9, 0), r, outbound=False)


def test_window_across_midnight():
    r = target(outbound_time="22:00-02:00")
    assert rules.passes(flight(23, 30), r)
    assert rules.passes(flight(1, 0), r)
    assert not rules.passes(flight(12, 0), r)


def test_matching_keeps_all_in_window_sorted_by_price():
    r = target(outbound_time="06:00-14:00")
    flights = [flight(2, 30, price=2000), flight(8, 0, price=4000), flight(13, 0, price=3500)]
    assert [f.price for f in rules.matching(flights, r)] == [3500, 4000]


def test_max_stops():
    assert not rules.passes(flight(8, stops=1), target())
    assert rules.passes(flight(8, stops=1), target(max_stops=1))


def test_duration_and_layover_limits():
    f = flight(8, stops=1)  # 總時間 180 分、轉機 90 分
    assert not rules.passes(f, target(max_stops=1, max_total_duration_h=2.5))
    assert not rules.passes(f, target(max_stops=1, max_layover_h=1))
    assert not rules.passes(f, target(max_stops=1, min_layover_min=120))
    assert rules.passes(f, target(max_stops=1, max_total_duration_h=3, max_layover_h=1.5, min_layover_min=90))
