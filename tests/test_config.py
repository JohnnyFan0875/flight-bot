import pytest
from pydantic import ValidationError

from flight_bot.config import Config, load_config


def test_repo_config_loads():
    cfg = load_config("config.yaml")
    dests = [t.dest for t in cfg.targets()]
    assert dests == ["CTS", "SDJ", "NRT", "HND"]


def test_override_order_destination_over_group_over_defaults():
    cfg = Config.model_validate({
        "defaults": {"origins": ["TPE", "TSA"], "search_to_days": 200},
        "groups": {
            "g": {
                "mode": "one_way",
                "trip_length_days": [5, 7],
                "origins": ["TPE"],
                "destinations": [
                    {"code": "NRT"},
                    {"code": "CTS", "origins": ["TSA"], "trip_length_days": [4, 6]},
                    {"code": "SDJ", "enabled": False},
                ],
            }
        },
    })
    targets = {t.dest: t for t in cfg.targets()}
    assert set(targets) == {"NRT", "CTS"}
    assert targets["NRT"].origins == ("TPE",)
    assert targets["NRT"].trip_length_days == (5, 7)
    assert targets["NRT"].search_to_days == 200
    assert targets["CTS"].origins == ("TSA",)
    assert targets["CTS"].trip_length_days == (4, 6)


@pytest.mark.parametrize("bad", [
    {"groups": {"g": {"mode": "one_way", "destinations": [{"code": "nrt"}]}}},
    {"groups": {"g": {"mode": "one_way", "destinations": [{"code": "NRTX"}]}}},
    {"groups": {"g": {"mode": "one_way", "trip_length_days": [7, 5], "destinations": []}}},
    {"groups": {"g": {"mode": "one_way", "destinations": [{"code": "NRT", "typo_field": 1}]}}},
    {"defaults": {"search_from_days": 100, "search_to_days": 50}},
    {"scan": {"per_date_tiers": [{"until_days": 100, "every": 1}, {"until_days": 60, "every": 3}]}},
    {"alerts": {"unknown": True}},
])
def test_validation_errors(bad):
    with pytest.raises(ValidationError):
        Config.model_validate(bad)


def test_tiers_must_cover_latest_return_date():
    with pytest.raises(ValidationError, match="沒有涵蓋"):
        Config.model_validate({
            "scan": {"per_date_tiers": [{"until_days": 210, "every": 1}]},
            "groups": {"g": {"mode": "one_way", "trip_length_days": [5, 7], "destinations": [{"code": "NRT"}]}},
        })
