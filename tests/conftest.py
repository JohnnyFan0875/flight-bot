from pathlib import Path

import pytest

from flight_bot.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def make_config(**overrides) -> Config:
    data = {
        "groups": {
            "short_haul": {
                "mode": "one_way",
                "max_stops": 0,
                "trip_length_days": [5, 7],
                "destinations": [{"code": "NRT"}],
            }
        },
        "defaults": {"origins": ["TPE", "TSA"]},
        "scan": {"strategy": "calendar"},
        "runner": {"delay_seconds": [0, 0], "retries": 0},
    }
    data.update(overrides)
    return Config.model_validate(data)
