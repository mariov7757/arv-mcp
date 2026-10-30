"""
ratelimit.py — token-bucket limits on outbound requests.

Every real HTTP request to Realtor.com (including HomeHarvest's internal
paging) and to the Census geocoder draws one token. A caller that can't get a
token within its max wait fails fast with RateLimited, carrying how long until
one would be free, so the MCP tool can tell the user instead of hanging.
"""

import os
import threading
import time


class RateLimited(Exception):
    """No token within the allowed wait. `retry_after_s` is the wait that
    would have been needed."""

    def __init__(self, retry_after_s: float, what: str = "requests"):
        self.retry_after_s = round(retry_after_s, 1)
        super().__init__(f"Local rate limit reached for {what}; retry in ~{self.retry_after_s}s.")


class TokenBucket:
    """Thread-safe token bucket: `rate_per_s` tokens refill per second, up to
    `burst` banked. `clock` and `sleep` are injectable for tests."""

    def __init__(self, rate_per_s: float, burst: int, *, name: str = "requests",
                 clock=time.monotonic, sleep=time.sleep):
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate_per_s must be > 0 and burst >= 1")
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate_per_s)
        self._updated = now

    def acquire(self, max_wait_s: float) -> None:
        """Take one token, sleeping up to `max_wait_s` for it. Raises
        RateLimited (without consuming anything) if the wait would be longer."""
        with self._lock:
            now = self._clock()
            self._refill(now)
            wait = 0.0 if self._tokens >= 1 else (1 - self._tokens) / self.rate_per_s
            if wait > max_wait_s:
                raise RateLimited(wait, self.name)
            # Reserve the token now (possibly going negative) so concurrent
            # callers queue behind it instead of all waking for the same token.
            self._tokens -= 1
        if wait > 0:
            self._sleep(wait)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def max_wait_s() -> float:
    return _env_float("ARV_MCP_RATE_WAIT_S", 15.0)


realtor_limiter = TokenBucket(
    _env_float("ARV_MCP_REQUESTS_PER_MIN", 30.0) / 60.0,
    int(_env_float("ARV_MCP_REQUEST_BURST", 10)),
    name="Realtor.com requests",
)
geocoder_limiter = TokenBucket(
    _env_float("ARV_MCP_GEOCODER_PER_SEC", 1.0),
    1,
    name="geocoder requests",
)
