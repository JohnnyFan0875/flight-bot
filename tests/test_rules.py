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


def test_matching_sorted_by_price():
    flights = [flight(2, 30, price=2000), flight(8, 0, price=4000), flight(13, 0, price=3500)]
    assert [f.price for f in rules.matching(flights, target())] == [2000, 3500, 4000]


def test_max_stops():
    assert not rules.passes(flight(8, stops=1), target())
    assert rules.passes(flight(8, stops=1), target(max_stops=1))


def test_duration_and_layover_limits():
    f = flight(8, stops=1)  # 總時間 180 分、轉機 90 分
    assert not rules.passes(f, target(max_stops=1, max_total_duration_h=2.5))
    assert not rules.passes(f, target(max_stops=1, max_layover_h=1))
    assert not rules.passes(f, target(max_stops=1, min_layover_min=120))
    assert rules.passes(f, target(max_stops=1, max_total_duration_h=3, max_layover_h=1.5, min_layover_min=90))
