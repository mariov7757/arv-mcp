"""Tests for ratelimit.py — token bucket with a fake clock."""
import pytest

from arv_mcp.ratelimit import RateLimited, TokenBucket


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def _bucket(rate_per_s=1.0, burst=3):
    clock = FakeClock()
    return TokenBucket(rate_per_s, burst, clock=clock, sleep=clock.sleep), clock


def test_burst_is_free():
    bucket, clock = _bucket(burst=3)
    for _ in range(3):
        bucket.acquire(max_wait_s=0)
    assert clock.slept == []


def test_waits_for_refill_within_max_wait():
    bucket, clock = _bucket(rate_per_s=0.5, burst=1)
    bucket.acquire(0)
    bucket.acquire(max_wait_s=5)
    assert clock.slept == [pytest.approx(2.0)]


def test_fails_fast_with_retry_after_when_wait_too_long():
    bucket, clock = _bucket(rate_per_s=0.1, burst=1)
    bucket.acquire(0)
    with pytest.raises(RateLimited) as exc:
        bucket.acquire(max_wait_s=1)
    assert exc.value.retry_after_s == pytest.approx(10.0)
    assert clock.slept == []


def test_refused_acquire_consumes_nothing():
    bucket, clock = _bucket(rate_per_s=1.0, burst=1)
    bucket.acquire(0)
    with pytest.raises(RateLimited):
        bucket.acquire(max_wait_s=0)
    clock.now += 1.0
    bucket.acquire(max_wait_s=0)  # the refused call didn't eat the refilled token


def test_queued_callers_each_wait_their_turn():
    bucket, clock = _bucket(rate_per_s=1.0, burst=1)
    bucket.acquire(0)
    # Two reservations in a row without the clock moving: the second must wait
    # behind the first (tokens go negative), not both wake for one token.
    bucket._sleep = lambda s: clock.slept.append(s)
    bucket.acquire(10)
    bucket.acquire(10)
    assert clock.slept == [pytest.approx(1.0), pytest.approx(2.0)]


def test_refill_caps_at_burst():
    bucket, clock = _bucket(rate_per_s=1.0, burst=2)
    clock.now += 100
    bucket.acquire(0)
    bucket.acquire(0)
    with pytest.raises(RateLimited):
        bucket.acquire(0)


def test_invalid_config():
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
    with pytest.raises(ValueError):
        TokenBucket(1, 0)
