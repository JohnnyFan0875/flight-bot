from collections import Counter
from datetime import date

from flight_bot import planner

from .conftest import make_config, route

TODAY = date(2026, 10, 24)


def test_every_day_in_range():
    cfg = make_config(destinations=[route(origins=["TPE"], query_start="2026-12-01", query_end="2026-12-10",
                                          trip_days=[3, 5])])  # 回程 12/1 + 2 到 12/10 + 4
    tasks = planner.per_date_tasks(cfg, TODAY)
    out = sorted(t.depart for t in tasks if t.origin == "TPE")
    back = sorted(t.depart for t in tasks if t.origin == "NRT")
    assert out[0] == date(2026, 12, 1) and out[-1] == date(2026, 12, 10) and len(out) == 10
    assert back[0] == date(2026, 12, 3) and back[-1] == date(2026, 12, 14) and len(back) == 12


def test_past_dates_skipped():
    cfg = make_config(destinations=[route(origins=["TPE"], query_start="2026-10-01", query_end="2026-10-30")])
    tasks = planner.per_date_tasks(cfg, TODAY)
    assert min(t.depart for t in tasks) == TODAY
    assert max(t.depart for t in tasks if t.origin == "TPE") == date(2026, 10, 30)


def test_shared_pairs_are_queried_once_with_max_stops():
    cfg = make_config(destinations=[
        route(name="a", origins=["TPE"], dests=["HND"], trip_days=[5]),
        route(name="b", origins=["TPE", "TSA"], dests=["HND"], trip_days=[3, 7]),
        route(name="c", origins=["TPE"], dests=["HND"], trip_days=[6], max_stops=1),
    ])
    tasks = planner.per_date_tasks(cfg, TODAY)
    keys = Counter((t.origin, t.dest, t.depart) for t in tasks)
    assert max(keys.values()) == 1
    assert {t.max_stops for t in tasks if t.origin == "TPE"} == {1}
    assert {t.max_stops for t in tasks if t.origin == "TSA"} == {0}
