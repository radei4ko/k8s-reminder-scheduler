"""When to make the next attempt after a transient failure."""

import random
from typing import Optional

from app.config import RETRY_BASE_DELAY_SECONDS, RETRY_MAX_DELAY_SECONDS


def next_delay_seconds(
    attempt_number: int,
    base_delay: int = RETRY_BASE_DELAY_SECONDS,
    max_delay: int = RETRY_MAX_DELAY_SECONDS,
    rng: Optional[random.Random] = None,
) -> int:
    """Exponential backoff, equal jitter.

    Equal jitter (half fixed, half random) guarantees a minimum delay while
    still spreading a batch of simultaneous failures out over time, instead of
    all of them coming back in one wave and re-triggering the same outage.
    """
    if attempt_number < 1:
        raise ValueError("attempt_number starts at 1")

    rng = rng or random.Random()
    delay = min(base_delay * (2 ** (attempt_number - 1)), max_delay)
    half = delay // 2
    return half + rng.randint(0, delay - half)
