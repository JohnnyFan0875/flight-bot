from collections import Counter
from datetime import date, timedelta

from flight_bot import planner
from flight_bot.config import Config

from .conftest import make_config

TODAY = date(2026, 10, 24)


def test_split_range_boundaries():
    start = date(2027, 1, 1)
    assert planner.split_range(start, start + timedelta(days=179), 180) == [(start, start + timedelta(days=179))]
    parts = planner.split_range(start, start + timedelta(days=180), 180)  # 181 天
    assert len(parts) == 2
    assert parts[0][0] == start and parts[-1][1] == start + timedelta(days=180)
    assert sum((b - a).days + 1 for a, b in parts) == 181
    # 段與段之間不重疊、不遺漏
    assert parts[1][0] == parts[0][1] + timedelta(days=1)


def test_return_leg_range_extends_by_trip_length():
    t = make_config().targets()[0]
    assert planner.depart_range(t, TODAY) == (TODAY + timedelta(days=30), TODAY + timedelta(days=210))
    assert planner.return_leg_range(t, TODAY) == (TODAY + timedelta(days=35), TODAY + timedelta(days=217))


def test_calendar_tasks_one_way():
    cfg = make_config()
    tasks = planner.calendar_tasks(cfg, TODAY)
    # 2 個出發地 × 2 個方向 × 2 段
    assert len(tasks) == 8
    assert all((t.end - t.start).days + 1 <= 180 for t in tasks)
    back = [t for t in tasks if t.leg.origin == "NRT"]
    assert max(t.end for t in back) == TODAY + timedelta(days=217)


def test_calendar_tasks_round_trip_every_length():
    cfg = Config.model_validate({
        "groups": {"long": {"mode": "round_trip", "origins": ["TPE"], "max_stops": 1,
                            "trip_length_days": [10, 14], "destinations": [{"code": "LHR"}]}},
    })
    tasks = planner.calendar_tasks(cfg, TODAY)
    assert sorted({t.trip_length for t in tasks}) == [10, 11, 12, 13, 14]
    assert len(tasks) == 5 * 2


def test_tier_boundaries():
    tiers = make_config().scan.per_date_tiers
    assert planner.tier_for(60, tiers).every == 1
    assert planner.tier_for(61, tiers).every == 3
    assert planner.tier_for(120, tiers).every == 3
    assert planner.tier_for(121, tiers).every == 10
    assert planner.tier_for(218, tiers) is None


def test_rotation_covers_every_date_once_per_cycle():
    tiers = make_config().scan.per_date_tiers
    target = TODAY + timedelta(days=150)  # 每 10 天一次的那層
    hits = [d for d in range(10) if planner.due_today(target, TODAY + timedelta(days=d), tiers)]
    assert len(hits) == 1


def test_per_date_daily_load_is_even():
    cfg = make_config()
    counts = [len(planner.per_date_tasks(cfg, TODAY + timedelta(days=i))) for i in range(21)]
    assert max(counts) - min(counts) <= 8


def test_per_date_respects_weekdays_only_for_outbound():
    cfg = make_config(defaults={"origins": ["TPE"], "depart_weekdays": [4]})  # 只週五出發
    tasks = planner.per_date_tasks(cfg, TODAY)
    out = [t for t in tasks if t.leg.outbound]
    back = [t for t in tasks if not t.leg.outbound]
    assert out and all(t.depart.weekday() == 4 for t in out)
    assert Counter(t.depart.weekday() for t in back).keys() != {4}
