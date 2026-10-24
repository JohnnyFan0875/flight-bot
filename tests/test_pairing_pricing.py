from datetime import date, timedelta

from flight_bot.config import AlertsConfig
from flight_bot.pairing import Combo, best_per_month, pair_one_way, pair_round_trip
from flight_bot.pricing import Baseline, baseline, evaluate, should_notify
from flight_bot.storage.state import State

from .conftest import make_config

TODAY = date(2026, 10, 24)
D = TODAY + timedelta(days=40)


def target(**defaults):
    return make_config(defaults=defaults).targets()[0] if defaults else make_config().targets()[0]


def q(*pairs):
    return {(d, None): p for d, p in pairs}


def test_pair_trip_length_bounds():
    t = target(origins=["TPE"])
    outbound = {"TPE": q((D, 3000))}
    inbound = {"TPE": q(*[(D + timedelta(days=n), 3000 + n) for n in range(3, 10)])}
    combos = pair_one_way(t, outbound, inbound, TODAY)
    assert sorted(c.stay_days for c in combos) == [5, 6, 7]


def test_pair_mixed_origins():
    t = target(origins=["TPE", "TSA"], allow_mixed_origins=True)
    outbound = {"TPE": q((D, 3000)), "TSA": q((D, 5000))}
    inbound = {"TPE": q((D + timedelta(days=5), 6000)), "TSA": q((D + timedelta(days=5), 2000))}
    [c] = pair_one_way(t, outbound, inbound, TODAY)
    assert (c.out_origin, c.back_origin, c.price) == ("TPE", "TSA", 5000)
    assert c.origins == "TPE-TSA"

    t = target(origins=["TPE", "TSA"], allow_mixed_origins=False)
    [c] = pair_one_way(t, outbound, inbound, TODAY)
    assert c.price == 7000  # TSA 去 TSA 回


def test_pair_skips_out_of_range_and_weekdays():
    t = target(origins=["TPE"], depart_weekdays=[D.weekday()])
    too_soon = TODAY + timedelta(days=10)
    other_day = D + timedelta(days=1)
    outbound = {"TPE": q((D, 3000), (too_soon, 1000), (other_day, 1000))}
    inbound = {"TPE": q(*[(x + timedelta(days=5), 3000) for x in (D, too_soon, other_day)])}
    combos = pair_one_way(t, outbound, inbound, TODAY)
    assert {c.depart for c in combos} == {D}


def test_pair_round_trip():
    cfg = make_config(groups={"long": {"mode": "round_trip", "trip_length_days": [10, 14],
                                       "origins": ["TPE"], "destinations": [{"code": "LHR"}]}})
    t = cfg.targets()[0]
    quotes = {"TPE": {(D, D + timedelta(days=10)): 30000, (D, D + timedelta(days=20)): 1000}}
    [c] = pair_round_trip(t, quotes, TODAY)
    assert c.price == 30000


def test_best_per_month_counts_nearby():
    t = target()
    combos = [Combo(t, D, D + timedelta(days=5), "TPE", "TPE", p) for p in (7850, 8000, 8600, 9000)]
    mb = best_per_month(combos)[D.strftime("%Y-%m")]
    assert mb.combo.price == 7850
    assert mb.nearby_price == 8700  # 7850 × 1.1 = 8635 → 8700
    assert mb.nearby == 2


def _history(state: State, t, prices_by_day: dict[int, int]):
    for days_ago, price in prices_by_day.items():
        state.add_history(TODAY - timedelta(days=days_ago), [(t.dest, t.mode, D, D + timedelta(days=5), "TPE", price)])


def test_warmup_blocks_new_low_and_median():
    t = target()
    state = State()
    _history(state, t, {3: 9000, 2: 9000, 1: 9000})
    combo = Combo(t, D, D + timedelta(days=5), "TPE", "TPE", 5000)
    base = baseline(state, combo, TODAY, AlertsConfig(warmup_days=7))
    assert not base.warm
    assert not evaluate(5000, combo, base, AlertsConfig()).hit


def test_new_low_and_below_median():
    t = target()
    state = State()
    _history(state, t, {d: 10000 for d in range(1, 11)} | {20: 8000})
    combo = Combo(t, D, D + timedelta(days=5), "TPE", "TPE", 7900)
    base = baseline(state, combo, TODAY, AlertsConfig(warmup_days=7))
    assert base.warm and base.lowest == 8000 and base.median == 10000
    ev = evaluate(7900, combo, base, AlertsConfig())
    assert ev.triggers == ["new_low", "below_median"]
    assert round(ev.pct_below_median) == 21

    ev = evaluate(8900, combo, base, AlertsConfig())
    assert ev.triggers == []  # 只低 11%，也不是新低


def test_google_low_needs_all_legs_low():
    t = target()
    combo = Combo(t, D, D + timedelta(days=5), "TPE", "TPE", 7000)
    base = Baseline(None, None, warm=False)
    assert evaluate(7000, combo, base, AlertsConfig(), ["low", "low"]).triggers == ["google_low"]
    assert not evaluate(7000, combo, base, AlertsConfig(), ["low", "typical"]).hit
    assert not evaluate(7000, combo, base, AlertsConfig(use_google_price_level=False), ["low", "low"]).hit


def test_max_price_by_month():
    t = target()
    combo = Combo(t, D, D + timedelta(days=5), "TPE", "TPE", 7000)
    alerts = AlertsConfig.model_validate({"max_price": {"default": 6000, "by_month": {D.month: 7500}}})
    assert evaluate(7000, combo, Baseline(None, None, False), alerts).triggers == ["max_price"]


def test_renotify_drop():
    t = target()
    state = State()
    combo = Combo(t, D, D + timedelta(days=5), "TPE", "TPE", 7000)
    assert should_notify(state, combo, 7000, 300)
    state.mark_notified(combo.key, 7000)
    assert not should_notify(state, combo, 6800, 300)
    assert should_notify(state, combo, 6700, 300)
