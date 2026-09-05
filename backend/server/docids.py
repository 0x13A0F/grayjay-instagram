"""Self-healing GraphQL doc_id registry.

Instagram's web client never sends GraphQL query text. Every query is
registered server-side at build time and addressed by a numeric `doc_id`
("persisted query"). We impersonate that client, so we must send the same
ids - and Instagram RETIRES them whenever it ships a new web build. The id
is global (identical for every account on earth), so a rotation breaks
every deployment of this backend at the same moment.

Rather than hardcode them and cut a release each time, we read the ids off
the real page: `browser.discover_doc_ids()` loads a genuine search SERP and
watches which doc_id Instagram's own JS posts. Learned ids are cached in
Redis so a restart doesn't re-trigger discovery.

Resolution order (first hit wins):
    1. env override   - operator pins an id by hand
    2. learned        - harvested from the live page (memory, then Redis)
    3. built-in       - the id that worked when this was written

So a fresh deploy works with zero configuration, and a rotation costs one
extra page load instead of a new image.
"""

import asyncio
import os
import time
from typing import Dict, Optional

import cache

# Friendly names are Instagram's own labels for each query; they're what we
# key everything on, since the doc_id is the thing that changes.
SEARCH = "PolarisKeywordSearchExplorePageRelayQuery"
SEARCH_PAGE = "PolarisKeywordSearchExplorePageRelayPaginationQuery"
SAVED = "PolarisProfileSavedTabContentQuery"

# Last-known-good ids, captured 2026-07. Only used until discovery runs.
DEFAULTS = {
    SEARCH: "27261995973455813",
    SEARCH_PAGE: "27606696395582173",
    SAVED: "27125139683774183",
}

# Manual pins. `or` (not getenv's default): compose passes these through as
# EMPTY strings when they aren't set in .env, which would blank the id out.
ENV_VARS = {
    SEARCH: "IG_DOC_ID_SEARCH",
    SEARCH_PAGE: "IG_DOC_ID_SEARCH_PAGE",
    SAVED: "IG_DOC_ID_SAVED",
}

_REDIS_PREFIX = "igdocid:v1:"
# Long: a learned id stays valid until Instagram's next web release.
LEARNED_TTL = int(os.getenv("IG_DOCID_TTL", str(14 * 24 * 3600)))
# Discovery drives the real browser, so rate-limit it hard. A burst of failing
# searches must not turn into a burst of page loads on the account.
DISCOVERY_COOLDOWN = float(os.getenv("IG_DOCID_DISCOVERY_COOLDOWN", "900"))

_learned: Dict[str, str] = {}
_last_discovery = 0.0


def env_override(friendly: str) -> str:
    return (os.getenv(ENV_VARS.get(friendly, "")) or "").strip()


async def resolve(friendly: str) -> str:
    """The doc_id to use for `friendly` right now."""
    pinned = env_override(friendly)
    if pinned:
        return pinned
    if friendly in _learned:
        return _learned[friendly]
    raw = await cache.get(_REDIS_PREFIX + friendly)
    if raw:
        value = raw.decode("utf-8", "ignore").strip()
        if value:
            _learned[friendly] = value
            return value
    return DEFAULTS.get(friendly, "")


def observe(friendly: str, doc_id: str) -> None:
    """Record a doc_id seen on a real request from Instagram's own JS.

    Called from a Playwright event handler (sync), so the Redis write is
    fired off as a task and never awaited.
    """
    if not friendly or not doc_id or _learned.get(friendly) == doc_id:
        return
    _learned[friendly] = doc_id
    print(f"doc_id learned: {friendly} = {doc_id}")
    try:
        asyncio.get_running_loop().create_task(_persist(friendly, doc_id))
    except RuntimeError:  # no loop (tests) - memory-only is fine
        pass


async def _persist(friendly: str, doc_id: str) -> None:
    try:
        await cache.set(
            _REDIS_PREFIX + friendly, doc_id.encode("utf-8"), LEARNED_TTL)
    except Exception as e:
        print(f"doc_id persist failed (ignored): {e}")


def discovery_allowed() -> bool:
    return (time.monotonic() - _last_discovery) >= DISCOVERY_COOLDOWN


def mark_discovery() -> None:
    global _last_discovery
    _last_discovery = time.monotonic()


def cooldown_remaining() -> int:
    elapsed = time.monotonic() - _last_discovery
    return max(0, int(DISCOVERY_COOLDOWN - elapsed))


async def snapshot() -> Dict[str, Dict[str, Optional[str]]]:
    """Current id per query plus where it came from (for /docids)."""
    out = {}
    for friendly in DEFAULTS:
        pinned = env_override(friendly)
        if pinned:
            source, value = "env", pinned
        elif friendly in _learned:
            source, value = "learned", _learned[friendly]
        else:
            value = await resolve(friendly)
            source = "learned" if friendly in _learned else "builtin"
        out[friendly] = {"doc_id": value, "source": source}
    return out
