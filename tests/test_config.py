from datetime import date

import pytest
from pydantic import ValidationError

from flight_bot.config import Config, load_config

from .conftest import route


def test_repo_config_loads():
    cfg = load_config("config.yaml")
    assert not cfg.destinations[0].enabled  # 第一個是說明用的範例
    routes = cfg.routes()
    assert [r.name for r in routes] == ["北海道", "仙台", "東京", "東京羽田（含松山）", "福岡"]
    tokyo = routes[2]
    assert tokyo.dests == ("NRT", "HND") and tokyo.trip_days == (5, 6, 7)
    assert tokyo.query_start <= tokyo.query_end


def test_enabled_and_per_route_rules():
    cfg = Config.model_validate({"destinations": [
        route(name="a", max_stops=1, max_total_duration_h=24, max_layover_h=6, min_layover_min=75),
        route(name="b"),
        route(name="c", enabled=False),
    ]})
    routes = {r.name: r for r in cfg.routes()}
    assert set(routes) == {"a", "b"}
    a, b = routes["a"], routes["b"]
    assert (a.max_stops, a.max_total_duration_h, a.max_layover_h, a.min_layover_min) == (1, 24, 6, 75)
    assert (b.max_stops, b.max_total_duration_h, b.max_layover_h, b.min_layover_min) == (0, None, None, None)


def test_route_defaults_and_date_ranges():
    cfg = Config.model_validate({"destinations": [
        {"origins": ["TPE", "TSA"], "dests": ["NRT", "HND"], "trip_days": [7, 5, 5],
         "query_start": "2026-10-01", "query_end": "2027-02-28"},
    ]})
    [r] = cfg.routes()
    assert r.name == "東京・成田、東京・羽田"
    assert r.trip_days == (5, 7)  # 排序、去重
    assert len(r.legs()) == 8 and ("NRT", "TSA", False) in r.legs()
    # 今天以前的日期不查
    assert r.date_range(True, date(2026, 10, 25)) == (date(2026, 10, 25), date(2027, 2, 28))
    assert r.date_range(False, date(2026, 9, 1)) == (date(2026, 10, 5), date(2027, 3, 6))


@pytest.mark.parametrize("bad", [
    {"destinations": [route(dests=["nrt"])]},
    {"destinations": [route(origins=[])]},
    {"destinations": [route(trip_days=[])]},
    {"destinations": [route(trip_days=[0, 5])]},
    {"destinations": [route(dests=["TPE"])]},
    {"destinations": [route(typo_field=1)]},
    {"destinations": [route(), route()]},  # 名稱重複
    {"destinations": [route(query_start="2027-03-01", query_end="2027-02-01")]},
    {"destinations": [route(query_start="明天")]},
    {"destinations": [{"origins": ["TPE"], "dests": ["NRT"], "trip_days": [5]}]},  # 缺 query_start
    {"destinations": [route(max_stops=3)]},
    {"defaults": {"search_from_days": 30}},  # 已移除
    {"groups": {}},  # 已移除
    {"alerts": {"unknown": True}},
])
def test_validation_errors(bad):
    with pytest.raises(ValidationError):
        Config.model_validate(bad)

