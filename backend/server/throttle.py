"""Outbound request pacing for Instagram.

Every real Instagram call goes through one browser page, but nothing used to
control how FAST they went out - Grayjay asks for a page, gets it, and asks
for the next one immediately. Bursts like that are what earn a 429 and the
"we suspect automated behaviour" notice, so pacing is mandatory here rather
than advisory: the plugin can ask for a LARGER gap, never a smaller one.

Three things happen in `wait()`:
  * a floor between consecutive calls (MIN_INTERVAL_FLOOR), which no caller
    can undercut;
  * jitter, because a request landing exactly every 2.000s is itself a
    signal that no human is driving;
  * a penalty window after a 429 or a login wall - exponential backoff that
    decays once calls start succeeding again.

Pacing happens on the outbound side only. Cache hits never reach it, so
repeat browsing stays instant.
"""

import asyncio
import contextvars
import os
import random
import time

# The hard floor. A caller asking for less gets this. Instagram's web client
# is bursty, but sustained sub-second automation is what trips the limiter.
MIN_INTERVAL_FLOOR = float(os.getenv("IG_MIN_INTERVAL_FLOOR", "1.0"))
# Used when a request carries no X-Min-Interval header.
DEFAULT_INTERVAL = float(os.getenv("IG_MIN_INTERVAL", "2.0"))
# Sanity cap, so a bad header can't wedge the backend for an hour.
MAX_INTERVAL = float(os.getenv("IG_MAX_INTERVAL", "60.0"))
# +/- this fraction of the interval, so the spacing isn't machine-regular.
JITTER = float(os.getenv("IG_INTERVAL_JITTER", "0.25"))

# After a 429/login wall: wait this long, doubling per consecutive hit.
BACKOFF_BASE = float(os.getenv("IG_BACKOFF_BASE", "60.0"))
BACKOFF_MAX = float(os.getenv("IG_BACKOFF_MAX", "900.0"))

# Longest we'll hold a client's request open while pacing. Beyond this we
# answer 429 + Retry-After instead of parking the connection: a backoff can
# run for minutes, and an HTTP client that waits that long just times out
# (Grayjay reports a 408) - which tells the user nothing and, worse, wedges
# every other request queued behind it.
MAX_WAIT = float(os.getenv("IG_MAX_WAIT", "15.0"))


# Set per request by the API middleware from the plugin's X-Min-Interval
# header, so browser.py can pace a call without the interval being threaded
# through every route and helper.
requested_interval = contextvars.ContextVar(
    "ig_requested_interval", default=None)


def clamp_interval(seconds) -> float:
    """Bound a requested interval to [floor, max]. Junk -> the default."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    if value != value or value <= 0:  # NaN or nonsense
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL_FLOOR, min(value, MAX_INTERVAL))


class RateLimited(Exception):
    """We're pacing/backing off for longer than a request can politely wait."""

    def __init__(self, retry_after: float):
        super().__init__(f"throttled; retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class Throttle:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._penalty_until = 0.0
        self._strikes = 0

    async def wait(self, interval=None, max_wait=None) -> float:
        """Block until it's polite to make the next Instagram call.

        Returns how long we waited (for logging). Serialized on its own lock
        so concurrent callers queue up instead of all reading a stale
        timestamp and firing at once.

        Raises RateLimited if the required delay is longer than `max_wait`
        (default MAX_WAIT) - i.e. we're in a backoff window. The caller
        should answer 429 rather than hold the connection open: parking a
        request for minutes just turns into a client-side timeout, and it
        blocks every request queued behind it too.
        """
        gap = clamp_interval(
            DEFAULT_INTERVAL if interval is None else interval)
        budget = MAX_WAIT if max_wait is None else max_wait
        async with self._lock:
            now = time.monotonic()
            spacing = gap * (1.0 + random.uniform(-JITTER, JITTER))
            ready_at = max(self._last + spacing, self._penalty_until)
            delay = ready_at - now
            if delay > budget:
                # Don't consume the slot: `_last` stays put so a request that
                # arrives once the window clears isn't penalised for this one.
                raise RateLimited(delay)
            if delay > 0:
                if self._penalty_until > now:
                    print(f"throttle: backing off {delay:.1f}s "
                          f"(rate-limit penalty, strike {self._strikes})")
                await asyncio.sleep(delay)
            self._last = time.monotonic()
            return max(0.0, delay)

    def penalize(self) -> float:
        """Instagram pushed back (429 or a login wall). Widen the gap."""
        self._strikes += 1
        backoff = min(BACKOFF_BASE * (2 ** (self._strikes - 1)), BACKOFF_MAX)
        self._penalty_until = max(
            self._penalty_until, time.monotonic() + backoff)
        print(f"throttle: rate-limited (strike {self._strikes}) - "
              f"pausing Instagram calls for {backoff:.0f}s")
        return backoff

    def relax(self) -> None:
        """A call succeeded: step the penalty back down one notch."""
        if self._strikes:
            self._strikes -= 1
            if not self._strikes:
                self._penalty_until = 0.0

    def clear(self) -> None:
        """Drop the penalty window (operator escape hatch)."""
        self._strikes = 0
        self._penalty_until = 0.0
        print("throttle: penalty cleared")

    def status(self) -> dict:
        remaining = max(0.0, self._penalty_until - time.monotonic())
        return {
            "min_interval_floor": MIN_INTERVAL_FLOOR,
            "default_interval": DEFAULT_INTERVAL,
            "strikes": self._strikes,
            "penalty_remaining": round(remaining, 1),
        }


throttle = Throttle()
