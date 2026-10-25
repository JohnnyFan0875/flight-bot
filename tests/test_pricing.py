from datetime import date, timedelta

from flight_bot.config import AlertsConfig
from flight_bot.pricing import Baseline, baseline, evaluate
from flight_bot.storage.state import State

TODAY = date(2026, 10, 24)
D = TODAY + timedelta(days=40)
ROUTE = "東京"


def _history(state: State, prices_by_day: dict[int, int]):
    for days_ago, price in prices_by_day.items():
        state.add_history(TODAY - timedelta(days=days_ago), [(ROUTE, "TPE", "NRT", D, price)])


def _base(state, warmup=7):
    return baseline(state, ROUTE, "TPE", "NRT", D.strftime("%Y-%m"), TODAY, AlertsConfig(warmup_days=warmup))


def test_warmup_blocks_new_low_and_median():
    state = State()
    _history(state, {3: 9000, 2: 9000, 1: 9000})
    base = _base(state)
    assert not base.warm
    assert not evaluate(5000, D, base, AlertsConfig()).hit


def test_new_low_and_below_median():
    state = State()
    _history(state, {d: 10000 for d in range(1, 11)} | {20: 8000})
    base = _base(state)
    assert base.warm and base.lowest == 8000 and base.median == 10000
    ev = evaluate(7900, D, base, AlertsConfig())
    assert ev.triggers == ["new_low", "below_median"]
    assert round(ev.pct_below_median) == 21
    assert evaluate(8900, D, base, AlertsConfig()).triggers == []  # 只低 11%，也不是新低


def test_baseline_is_per_route_and_direction():
    state = State()
    _history(state, {d: 10000 for d in range(1, 11)})
    assert baseline(state, ROUTE, "NRT", "TPE", D.strftime("%Y-%m"), TODAY, AlertsConfig()).median is None
    assert baseline(state, "other/x", "TPE", "NRT", D.strftime("%Y-%m"), TODAY, AlertsConfig()).median is None


def test_google_low_per_flight():
    base = Baseline(None, None, warm=False)
    assert evaluate(1900, D, base, AlertsConfig(), typical=(2000, 5000)).triggers == ["google_low"]
    assert not evaluate(2100, D, base, AlertsConfig(), typical=(2000, 5000)).hit
    # 沒有典型區間時，只有當天最便宜的那班看價格等級
    assert evaluate(2100, D, base, AlertsConfig(), level="low", cheapest=True).triggers == ["google_low"]
    assert not evaluate(2100, D, base, AlertsConfig(), level="low", cheapest=False).hit
    assert not evaluate(1900, D, base, AlertsConfig(use_google_price_level=False), typical=(2000, 5000)).hit


def test_max_price_by_month():
    alerts = AlertsConfig.model_validate({"max_price": {"default": 3000, "by_month": {D.month: 3500}}})
    assert evaluate(3400, D, Baseline(None, None, False), alerts).triggers == ["max_price"]


def test_search_keeps_previous_day_for_status():
    state = State()
    state.upsert_search("TPE", "NRT", D, 0, None, None, [{"a": 1}], TODAY)
    state.upsert_search("TPE", "NRT", D, 0, None, None, [{"a": 2}], TODAY + timedelta(days=1))
    row = state.get_search("TPE", "NRT", D)
    assert row["prev_flights_json"] == '[{"a": 1}]'
    # 同一天重跑：比較對象仍是前一天
    state.upsert_search("TPE", "NRT", D, 0, None, None, [{"a": 3}], TODAY + timedelta(days=1))
    row = state.get_search("TPE", "NRT", D)
    assert row["prev_flights_json"] == '[{"a": 1}]' and row["flights_json"] == '[{"a": 3}]'
