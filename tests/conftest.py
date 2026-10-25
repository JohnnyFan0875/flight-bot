from pathlib import Path

import pytest

from flight_bot.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def route(**overrides) -> dict:
    return {"name": "東京", "origins": ["TPE", "TSA"], "dests": ["NRT"], "query_start": "2026-11-01",
            "query_end": "2027-01-31", "trip_days": [5, 6, 7], **overrides}


def make_config(**overrides) -> Config:
    data = {"destinations": [route()], "runner": {"delay_seconds": [0, 0], "retries": 0}}
    data.update(overrides)
    return Config.model_validate(data)
