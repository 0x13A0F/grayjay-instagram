"""Short-lived response cache (Redis).

Grayjay re-issues the exact same backend request constantly (flipping pages,
re-opening a channel, re-running a search). Each one otherwise drives the real
browser - slow and a rate-limit risk. This caches 200 JSON responses in Redis
for a plugin-controlled window so repeats are served instantly.

Everything here is best-effort: if Redis is unreachable, get() returns None and
set() is a no-op, so the backend keeps serving (just uncached). A cache being
down must never break - or even slow down - a request.
"""

import hashlib
import os
import time
from typing import Optional

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Cap whatever TTL the plugin asks for, so a bad/hostile header can't pin a
# response forever.
CACHE_MAX_TTL = int(os.getenv("CACHE_MAX_TTL", "600"))
# TTL used when a request carries no X-Cache-TTL header (older plugin, or a
# direct call). 0 = don't cache unless the caller explicitly asks.
CACHE_DEFAULT_TTL = int(os.getenv("CACHE_DEFAULT_TTL", "0"))

# Fail fast if Redis is unreachable so a down cache doesn't add latency, and
# back off for a bit before trying again (a "circuit breaker") - but DO retry,
# so caching resumes on its own once Redis is back, no backend restart needed.
_SOCKET_TIMEOUT = float(os.getenv("CACHE_SOCKET_TIMEOUT", "0.5"))
_COOLDOWN_SECONDS = float(os.getenv("CACHE_COOLDOWN_SECONDS", "10"))

_KEY_PREFIX = "igcache:v1:"

_client: Optional[aioredis.Redis] = None
_skip_until = 0.0  # monotonic time; while now < this, skip Redis entirely.


def _get_client() -> Optional[aioredis.Redis]:
    global _client
    if time.monotonic() < _skip_until:
        return None
    if _client is None:
        try:
            _client = aioredis.from_url(
                REDIS_URL,
                socket_connect_timeout=_SOCKET_TIMEOUT,
                socket_timeout=_SOCKET_TIMEOUT,
            )
        except Exception as e:  # bad URL, etc.
            print(f"cache: cannot init Redis client ({e}); backing off.")
            _trip()
            return None
    return _client


def _trip():
    """Redis op failed - skip it for a cooldown window, then let it retry."""
    global _skip_until
    _skip_until = time.monotonic() + _COOLDOWN_SECONDS


def make_key(method: str, path: str, query: str) -> str:
    """Stable cache key from method + path + sorted query string. Hashed so the
    key length is bounded and query values (usernames, cursors) aren't stored
    in the clear as key names."""
    canonical_query = "&".join(sorted(query.split("&"))) if query else ""
    raw = f"{method}\n{path}\n{canonical_query}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return _KEY_PREFIX + digest


def clamp_ttl(ttl: Optional[int]) -> int:
    """Bound a requested TTL to [0, CACHE_MAX_TTL]. 0 means 'do not cache'."""
    if not ttl or ttl < 0:
        return 0
    return min(ttl, CACHE_MAX_TTL)


async def get(key: str) -> Optional[bytes]:
    client = _get_client()
    if client is None:
        return None
    try:
        return await client.get(key)
    except Exception:
        _trip()
        return None


async def set(key: str, value: bytes, ttl: int) -> None:
    if ttl <= 0:
        return
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(key, value, ex=ttl)
    except Exception:
        _trip()


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None
