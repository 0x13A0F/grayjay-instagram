"""Unit tests for the self-healing doc_id registry.

Run from the backend/ dir (PYTHONPATH points at the code in server/):
    PYTHONPATH=server python3 -m tests.test_docids

Covers the three things that must not regress:
  1. resolution order (env pin > learned > built-in), including the
     empty-env trap that compose's ${VAR:-} creates;
  2. the request listener only harvesting Instagram's OWN queries;
  3. a retired doc_id being relearned and the call retried exactly once,
     with the cooldown preventing a discovery storm.
Camoufox and the Redis-backed cache are stubbed, so this needs no browser,
no network and no Redis.
"""

import asyncio
import os
import sys
import types
from urllib.parse import urlencode

# --- stubs (must be installed before importing the modules under test) ---
_cache = types.ModuleType("cache")
_cache_store = {}


async def _cache_get(key):
    return _cache_store.get(key)


async def _cache_set(key, value, ttl):
    _cache_store[key] = value


_cache.get = _cache_get
_cache.set = _cache_set
sys.modules["cache"] = _cache

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
    sys.modules[_name] = _m

import docids  # noqa: E402
from browser import IGBrowser, IGError  # noqa: E402

# One loop for the whole run: Python 3.14 no longer creates an implicit one.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    return _LOOP.run_until_complete(coro)


def observe(friendly, doc_id):
    """docids.observe() from inside the loop, then let its cache-write task
    finish - it's fire-and-forget in production."""
    async def _go():
        docids.observe(friendly, doc_id)
        await asyncio.sleep(0.01)
    run(_go())


passed = 0


def check(name, cond):
    global passed
    assert cond, "FAILED: " + name
    passed += 1
    print("  ok:", name)


def gql_body(friendly, doc_id):
    return urlencode({
        "av": "1", "doc_id": doc_id, "variables": "{}",
        "fb_api_req_friendly_name": friendly, "fb_dtsg": "x",
    })


class FakeRequest:
    def __init__(self, method, url, post_data):
        self.method = method
        self.url = url
        self.post_data = post_data


def test_resolution_order():
    print("resolution order")
    docids._learned.clear()
    _cache_store.clear()

    check("built-in when nothing else is known",
          run(docids.resolve(docids.SEARCH)) == docids.DEFAULTS[docids.SEARCH])

    observe(docids.SEARCH, "learned-id")
    check("learned beats built-in",
          run(docids.resolve(docids.SEARCH)) == "learned-id")

    os.environ["IG_DOC_ID_SEARCH"] = "pinned-id"
    check("env pin beats learned",
          run(docids.resolve(docids.SEARCH)) == "pinned-id")

    # docker-compose passes ${IG_DOC_ID_SEARCH:-} through as an EMPTY string
    # when it isn't in .env; that must not blank the id out.
    os.environ["IG_DOC_ID_SEARCH"] = ""
    check("empty env falls through to learned",
          run(docids.resolve(docids.SEARCH)) == "learned-id")
    del os.environ["IG_DOC_ID_SEARCH"]

    # A learned id survives a restart via the cache.
    docids._learned.clear()
    check("learned id is read back from the cache",
          run(docids.resolve(docids.SEARCH)) == "learned-id")


def test_listener():
    print("request listener")
    docids._learned.clear()
    _cache_store.clear()
    b = IGBrowser()
    ig = "https://www.instagram.com"

    # Closed window: our own /api/graphql posts carry a possibly-stale id and
    # must never be learned back.
    b._on_request(FakeRequest(
        "POST", ig + "/api/graphql", gql_body(docids.SEARCH, "ours")))
    check("nothing harvested outside a discovery window",
          docids.SEARCH not in docids._learned)

    seen = {}
    b._harvest = seen
    b._on_request(FakeRequest(
        "POST", ig + "/api/graphql", gql_body(docids.SEARCH, "real")))
    b._on_request(FakeRequest(
        "POST", ig + "/graphql/query", gql_body(docids.SAVED, "saved")))
    check("harvests IG's own queries",
          seen == {docids.SEARCH: "real", docids.SAVED: "saved"})
    check("registers what it harvests",
          docids._learned[docids.SEARCH] == "real")

    before = dict(seen)
    b._on_request(FakeRequest("GET", ig + "/api/graphql?x=1", None))
    b._on_request(FakeRequest("POST", ig + "/api/v1/feed/", "amount=24"))
    b._on_request(FakeRequest("POST", ig + "/api/graphql", "novars=1"))
    # A third-party frame posting to its own /api/graphql must be ignored:
    # this is an origin check, not a substring match.
    b._on_request(FakeRequest(
        "POST", "https://evil.example/api/graphql",
        gql_body(docids.SEARCH, "poison")))
    check("ignores non-IG origins, other paths, GETs and bodyless posts",
          seen == before)

    class Exploding:
        method = "POST"

        @property
        def url(self):
            raise RuntimeError("boom")

    b._on_request(Exploding())  # must not raise into Playwright's loop
    check("a malformed request never raises", True)
    b._harvest = None


def test_relearn_and_retry():
    """The end-to-end recovery path, against the real app._graphql."""
    try:
        import app
    except ImportError as e:  # fastapi absent - skip rather than fail
        print("relearn/retry SKIPPED (%s)" % e)
        return
    print("relearn and retry")
    docids._learned.clear()
    _cache_store.clear()
    docids._last_discovery = 0.0

    live = "live-id"
    stale = docids.DEFAULTS[docids.SEARCH]
    sent = []

    class FakeBrowser:
        ready = True
        navigations = 0

        async def get_tokens(self):
            return {"dtsg": "d", "lsd": "l"}

        async def ig_fetch(self, path, params=None, method="GET",
                           form=None, headers=None):
            sent.append(form["doc_id"])
            if form["doc_id"] != live:
                # How IG actually rejects a retired id: a non-JSON envelope,
                # not a clean GraphQL error.
                raise IGError(200, 'non-JSON response: for (;;);'
                                   '{"__ar":1,"error":1357004}')
            return {"data": {"ok": True}}

        async def discover_doc_ids(self, query="reels"):
            FakeBrowser.navigations += 1
            docids.observe(docids.SEARCH, live)
            return {docids.SEARCH: live}

    app.browser = FakeBrowser()

    out = run(app._graphql("/api/graphql", docids.SEARCH, {"query": "x"}))
    check("call succeeds after a rotation", out == {"data": {"ok": True}})
    check("tried the stale id, then the relearned one",
          sent == [stale, live])
    check("discovery ran exactly once", FakeBrowser.navigations == 1)

    del sent[:]
    run(app._graphql("/api/graphql", docids.SEARCH, {"query": "y"}))
    check("later calls use the learned id with no retry", sent == [live])
    check("no further discovery", FakeBrowser.navigations == 1)

    # A storm of failures must not become a storm of page loads.
    docids._learned.clear()
    _cache_store.clear()
    del sent[:]
    for _ in range(5):
        try:
            run(app._graphql("/api/graphql", docids.SEARCH, {"query": "z"}))
        except IGError:
            pass
    check("cooldown blocks repeat discoveries",
          FakeBrowser.navigations == 1)
    check("each failure is attempted once, no retry loop", len(sent) == 5)


if __name__ == "__main__":
    test_resolution_order()
    test_listener()
    test_relearn_and_retry()
    print("\n%d checks passed" % passed)
