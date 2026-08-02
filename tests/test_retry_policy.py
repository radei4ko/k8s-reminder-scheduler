import random

import pytest

from app.retry_policy import next_delay_seconds


def test_delay_grows_exponentially():
    rng = random.Random(42)
    for attempt, expected_delay in [(1, 10), (2, 20), (3, 40), (4, 80)]:
        value = next_delay_seconds(attempt, base_delay=10, max_delay=10_000, rng=rng)
        assert expected_delay // 2 <= value <= expected_delay


def test_delay_is_capped():
    rng = random.Random(1)
    value = next_delay_seconds(20, base_delay=10, max_delay=300, rng=rng)
    assert 150 <= value <= 300


def test_attempt_number_starts_from_one():
    with pytest.raises(ValueError):
        next_delay_seconds(0)
