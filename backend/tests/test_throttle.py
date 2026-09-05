"""Unit tests for outbound request pacing.

Run from the backend/ dir (PYTHONPATH points at the code in server/):
    PYTHONPATH=server python3 -m tests.test_throttle

Time and sleeping are faked, so this is deterministic and instant. What must
not regress:
  * the floor is a FLOOR - a client cannot ask us to go faster;
  * consecutive calls are actually spaced apart;
  * a 429 widens the gap, escalates while it keeps happening, and decays;
  * the plugin's X-Min-Interval header survives Starlette's middleware task
    boundary and reaches the code that paces the browser (a contextvar
    crossing tasks - easy to break silently).
"""

import asyncio
import sys
import types

# Stub the Redis-backed cache and Camoufox so this runs with no browser,
# no network and no Redis.
async def _noop_get(key):
    return None


async def _noop_set(key, value, ttl):
    return None


_cache = types.ModuleType("cache")
_cache.get = _noop_get
_cache.set = _noop_set
_cache.CACHE_DEFAULT_TTL = 0
_cache.CACHE_MAX_TTL = 600
_cache.clamp_ttl = lambda ttl: 0
_cache.make_key = lambda m, p, q: "k"
sys.modules.setdefault("cache", _cache)

for _name, _attrs in [
    ("camoufox", {}),
    ("camoufox.async_api", {"AsyncCamoufox": object}),
    ("camoufox.fingerprints", {"get_random_preset": lambda *a, **k: {}}),
    ("camoufox.pkgman", {"installed_verstr": lambda: "0.0"}),
]:
    _m = types.ModuleType(_name)
    if _name == "camoufox":
        _m.__path__ = []
    for _k, _v in _attrs.items():
        setattr(_m, _k, _v)
    sys.modules.setdefault(_name, _m)

import throttle as th  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    assert cond, "FAILED: " + name
    passed += 1
    print("  ok:", name)


class FakeClock:
    """Virtual time: sleeping jumps the clock instead of waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def with_fake_clock():
    """Swap throttle's time/asyncio for the fake clock; returns it."""
    clock = FakeClock()
    shim = types.SimpleNamespace(sleep=clock.sleep, Lock=asyncio.Lock)
    th.time = types.SimpleNamespace(monotonic=clock.monotonic)
    th.asyncio = shim
    return clock


def run(coro):
    return _LOOP.run_until_complete(coro)


_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def test_clamp():
    print("interval clamping")
    th.MIN_INTERVAL_FLOOR = 1.0
    th.DEFAULT_INTERVAL = 2.0
    th.MAX_INTERVAL = 60.0

    check("a faster-than-floor request is raised to the floor",
          th.clamp_interval(0.01) == 1.0)
    check("zero is not 'no throttling'", th.clamp_interval(0) == 2.0)
    check("negative falls back to the default",
          th.clamp_interval(-5) == 2.0)
    check("garbage falls back to the default",
          th.clamp_interval("abc") == 2.0)
    check("None falls back to the default", th.clamp_interval(None) == 2.0)
    check("NaN falls back to the default",
          th.clamp_interval(float("nan")) == 2.0)
    check("a larger request is honoured", th.clamp_interval(8) == 8.0)
    check("an absurd request is capped", th.clamp_interval(10 ** 6) == 60.0)


def test_spacing():
    print("call spacing")
    clock = with_fake_clock()
    th.JITTER = 0.0
    t = th.Throttle()

    run(t.wait(2.0))
    check("the first call isn't delayed", clock.slept == [])
    run(t.wait(2.0))
    check("the second call waits the full interval", clock.slept == [2.0])
    clock.now += 5.0  # user idle - the gap is already satisfied
    run(t.wait(2.0))
    check("an idle gap counts, no extra wait", clock.slept == [2.0])

    # A burst of callers must queue, not all fire at once.
    clock.slept.clear()
    run(asyncio.gather(*(t.wait(2.0) for _ in range(4))))
    check("four concurrent callers are spaced, not batched",
          len(clock.slept) == 4 and all(s > 0 for s in clock.slept))


def test_floor_is_mandatory():
    print("floor is mandatory")
    clock = with_fake_clock()
    th.JITTER = 0.0
    th.MIN_INTERVAL_FLOOR = 1.0
    t = th.Throttle()
    run(t.wait(0.001))
    run(t.wait(0.001))
    check("a client asking for 1ms still gets the floor",
          clock.slept == [1.0])


def test_jitter():
    print("jitter")
    clock = with_fake_clock()
    th.JITTER = 0.25
    t = th.Throttle()
    for _ in range(40):
        run(t.wait(2.0))
        clock.now = clock.now  # no idle time between calls
    waits = clock.slept
    check("every wait is within the jitter band",
          all(1.5 - 1e-9 <= w <= 2.5 + 1e-9 for w in waits))
    check("spacing is not machine-regular", len(set(waits)) > 1)
    th.JITTER = 0.0


def test_backoff():
    print("rate-limit backoff")
    clock = with_fake_clock()
    th.JITTER = 0.0
    th.BACKOFF_BASE = 60.0
    th.BACKOFF_MAX = 900.0
    t = th.Throttle()
    run(t.wait(2.0))

    check("first strike backs off the base", t.penalize() == 60.0)
    check("it escalates while it keeps happening", t.penalize() == 120.0)
    check("and is capped", [t.penalize() for _ in range(10)][-1] == 900.0)

    clock.slept.clear()
    run(t.wait(2.0))
    check("the next call serves the penalty, not the interval",
          clock.slept and clock.slept[0] > 60.0)
    check("penalty is reported", t.status()["penalty_remaining"] >= 0)

    for _ in range(20):
        t.relax()
    check("success decays the penalty entirely",
          t.status()["strikes"] == 0
          and t.status()["penalty_remaining"] == 0)


def test_header_reaches_the_browser():
    """X-Min-Interval must survive Starlette's middleware task boundary."""
    try:
        import httpx
        import app
    except ImportError as e:
        print("header plumbing SKIPPED (%s)" % e)
        return
    print("header plumbing")
    seen = []

    class FakeBrowser:
        ready = True
        needs_login = False

        async def ig_fetch(self, path, params=None, method="GET",
                           form=None, headers=None):
            # Read the contextvar exactly where browser.py reads it.
            seen.append(th.requested_interval.get())
            return {"users": []}

    app.browser = FakeBrowser()

    async def go():
        tr = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            await c.get("/search/users?query=x",
                        headers={"X-Min-Interval": "8"})
            await c.get("/search/users?query=y")
            await c.get("/search/users?query=z",
                        headers={"X-Min-Interval": "0.001"})
            await c.get("/search/users?query=w",
                        headers={"X-Min-Interval": "junk"})
    run(go())

    check("a larger requested interval reaches the browser layer",
          seen[0] == 8.0)
    check("no header -> the configured default",
          seen[1] == th.DEFAULT_INTERVAL)
    check("a too-small request is raised to the floor",
          seen[2] == th.MIN_INTERVAL_FLOOR)
    check("a junk header falls back to the default",
          seen[3] == th.DEFAULT_INTERVAL)


if __name__ == "__main__":
    test_clamp()
    test_spacing()
    test_floor_is_mandatory()
    test_jitter()
    test_backoff()
    test_header_reaches_the_browser()
    print("\n%d checks passed" % passed)
