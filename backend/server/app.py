"""Small REST API for the Grayjay Instagram plugin. The session is the
browser profile.

Routes:
  GET /health
  GET /search/users?query=
  GET /user?username=   |  /user?user_id=
  GET /feed?amount=&cursor=                      (logged-in home timeline)
  GET /user/reels?username=&amount=&cursor=
  GET /media?code=
  GET /media/comments?media_id=&amount=&cursor=
  GET /media/comments/replies?media_id=&comment_id=&cursor=
  GET /following?cursor=                          (logged-in account's follows)
"""

import normalize as N

import hmac
import json
import os
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote, urlencode
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
import cache
from browser import IGError, browser
from utils import pk_str, shortcode_to_pk

# Shared-secret gate. When IG_API_KEY is set, every request (except /health)
# must send a matching X-API-Key header - so a publicly-exposed backend can't
# be driven by anyone who finds it. Empty = open (local dev).
API_KEY = os.getenv("IG_API_KEY", "").strip()
_OPEN_PATHS = {"/health"}

# username (lower) -> pk, to avoid re-resolving on every reels page.
_pk_cache = {}
# The logged-in viewer's own pk (from the ds_user_id cookie), resolved once.
_viewer_pk_cache = {}

# GraphQL doc_ids for keyword reel search (xdt_fbsearch__top_serp_graphql).
# These ROTATE when Instagram updates its web app. If keyword search starts
# failing, re-capture from the browser (DevTools -> the request named
# PolarisKeywordSearchExplorePage*) and update these (or the env overrides).
DOC_ID_SEARCH = os.getenv("IG_DOC_ID_SEARCH", "27261995973455813")
DOC_ID_SEARCH_PAGE = os.getenv("IG_DOC_ID_SEARCH_PAGE", "27606696395582173")
# Saved-collections list (xdt_api__v1__collections__list_graphql_connection).
DOC_ID_SAVED_COLLECTIONS = os.getenv("IG_DOC_ID_SAVED", "27125139683774183")
ALL_SAVED_ID = "ALL_MEDIA_AUTO_COLLECTION"  # Instagram's "All posts" bucket


@asynccontextmanager
async def lifespan(app: FastAPI):
    if API_KEY:
        print("API key auth ENABLED (X-API-Key required).")
    else:
        print("WARNING: IG_API_KEY not set - backend is UNAUTHENTICATED. "
              "Set it before exposing this server publicly.")
    await browser.start()
    try:
        yield
    finally:
        await browser.stop()
        await cache.close()


app = FastAPI(title="Instagram (Camoufox) backend", lifespan=lifespan)


# Response cache. Registered BEFORE require_api_key so that - since Starlette
# runs the last-registered middleware first - the API-key gate stays OUTERMOST
# and an unauthenticated request can never read from the cache.
@app.middleware("http")
async def cache_responses(request: Request, call_next):
    # Only cache idempotent GETs; never /health. TTL is driven by the plugin
    # via X-Cache-TTL (seconds); absent/0/invalid -> bypass entirely.
    if request.method != "GET" or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    raw_ttl = request.headers.get("x-cache-ttl", "")
    try:
        requested = int(raw_ttl) if raw_ttl else cache.CACHE_DEFAULT_TTL
    except ValueError:
        requested = cache.CACHE_DEFAULT_TTL
    ttl = cache.clamp_ttl(requested)
    if ttl <= 0:
        return await call_next(request)

    target = f"{request.url.path}?{request.url.query}" if request.url.query \
        else request.url.path
    key = cache.make_key(request.method, request.url.path, request.url.query)
    hit = await cache.get(key)
    if hit is not None:
        print(f"cache HIT: {target}")
        return Response(content=hit, media_type="application/json",
                        headers={"X-Cache": "HIT"})

    response = await call_next(request)
    # Cache only successful JSON; errors (rate-limit walls, 404s, 422s) must
    # not get pinned. Draining body_iterator means we must rebuild the response.
    if response.status_code == 200:
        body = b"".join([chunk async for chunk in response.body_iterator])
        await cache.set(key, body, ttl)
        print(f"cache MISS: {target} (cached for {ttl}s)")
        headers = dict(response.headers)
        headers.pop("content-length", None)  # recomputed from the new body
        headers["X-Cache"] = "MISS"
        return Response(content=body, status_code=200, headers=headers)
    return response


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # request.url.path is the path only (no query string), so a param like
    # ?x=/health can't make a protected route look open. Exact match on the
    # open set; everything else needs a valid key.
    if API_KEY and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("x-api-key", "")
        # constant-time compare (bytes, so a non-ASCII header can't crash it).
        if not hmac.compare_digest(
                provided.encode("utf-8"), API_KEY.encode("utf-8")):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key",
                         "exc_type": "Unauthorized"},
            )
    return await call_next(request)


def _fail(err: IGError):
    code = err.status if err.status in (401, 403, 404, 429) else 500
    return JSONResponse(
        status_code=code,
        content={"detail": err.detail, "exc_type": "IGError"},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Anything a route's `except IGError` doesn't catch (a route-level bug, or
    # a non-IGError raised through browser.ig_fetch - e.g. a Playwright error
    # that isn't recognized as "browser dead", see browser.py's ig_fetch_raw)
    # used to surface as an opaque 500 with nothing in the logs. Print the
    # route + exception + full traceback so it's actually diagnosable.
    target = f"{request.url.path}?{request.url.query}" if request.url.query \
        else request.url.path
    print(f"UNHANDLED ERROR on GET {target}: {type(exc).__name__}: {exc}")
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "exc_type": type(exc).__name__},
    )


async def _graphql(path: str, doc_id: str, friendly: str, variables: dict):
    """POST a GraphQL doc_id query from the logged-in page. Instagram's
    GraphQL endpoints reject the request (HTML shell) without fb_dtsg + lsd,
    which we read out of the page."""
    tokens = await browser.get_tokens()
    dtsg = tokens.get("dtsg", "")
    lsd = tokens.get("lsd", "")
    form = {
        "doc_id": doc_id,
        "variables": json.dumps(variables),
        "fb_api_req_friendly_name": friendly,
        "server_timestamps": "true",
        "fb_dtsg": dtsg,
        "jazoest": "2" + str(sum(ord(c) for c in dtsg)) if dtsg else "",
        "lsd": lsd,
        "__a": "1",
        "__comet_req": "7",
    }
    headers = {"X-FB-Friendly-Name": friendly}
    if lsd:
        headers["X-FB-LSD"] = lsd
    return await browser.ig_fetch(
        path, method="POST", form=form, headers=headers)


async def _resolve_pk(username: str) -> str:
    key = username.lower().lstrip("@")
    if key in _pk_cache:
        return _pk_cache[key]
    resp = await browser.ig_fetch(
        "/api/v1/users/web_profile_info/", params={"username": key})
    pk = N.norm_user(N.user_from_web_profile(resp)).get("pk")
    if not pk:
        raise IGError(404, "user not found: " + username)
    _pk_cache[key] = pk
    return pk


async def _viewer_pk() -> str:
    """The logged-in account's own pk (cached). Raises if not logged in."""
    pk = _viewer_pk_cache.get("pk")
    if pk:
        return pk
    pk = await browser.viewer_id()
    if not pk:
        raise IGError(401, "not logged in (no ds_user_id cookie)")
    _viewer_pk_cache["pk"] = pk
    return pk


@app.get("/health")
async def health():
    # needs_login=True -> open noVNC (:6080 /vnc.html) and log in; serving
    # resumes automatically once the session is detected.
    return {"status": "ok", "ready": browser.ready,
            "needs_login": browser.needs_login}


@app.get("/following")
async def following(cursor: str = Query("")):
    # The logged-in account's "following" list (one page). The plugin paginates
    # through all pages to feed Grayjay's "Import subscriptions".
    try:
        pk = await _viewer_pk()
        params = {"count": 200}
        if cursor:
            params["max_id"] = cursor
        resp = await browser.ig_fetch(
            f"/api/v1/friendships/{pk}/following/", params=params)
        return N.norm_following_page(resp)
    except IGError as e:
        return _fail(e)


@app.get("/search/users")
async def search_users(
    query: str = Query(...),
):
    try:
        # account_serp is the dedicated account search (returns many users);
        # /web/search/topsearch/ only returns ~5 blended top results.
        resp = await browser.ig_fetch(
            "/api/v1/fbsearch/account_serp/",
            params={"query": query, "count": 50},
        )
        return N.norm_search_users(resp)
    except IGError as e:
        return _fail(e)


@app.get("/search/reels")
async def search_reels(
    query: str = Query(...),
    cursor: str = Query(""),
    session_id: str = Query(""),
):
    # Keyword reel search via the web app's GraphQL SERP query. session_id ties
    # paginated pages to one search session; the plugin generates it once per
    # search and passes it on every page (we fall back to a fresh uuid).
    try:
        sid = session_id or str(uuid.uuid4())
        if cursor:
            doc_id = DOC_ID_SEARCH_PAGE
            friendly = "PolarisKeywordSearchExplorePageRelayPaginationQuery"
            variables = {"after": cursor, "first": 24, "query": query,
                         "search_session_id": sid, "serp_session_id": sid}
        else:
            doc_id = DOC_ID_SEARCH
            friendly = "PolarisKeywordSearchExplorePageRelayQuery"
            variables = {"query": query,
                         "search_session_id": sid, "serp_session_id": sid}
        resp = await _graphql("/api/graphql", doc_id, friendly, variables)
        return N.norm_keyword_search_page(resp)
    except IGError as e:
        return _fail(e)


@app.get("/saved/collections")
async def saved_collections():
    # The logged-in account's saved collections (each -> a Grayjay playlist).
    try:
        variables = {
            "collection_types": [
                "ALL_MEDIA_AUTO_COLLECTION", "MEDIA", "AUDIO_AUTO_COLLECTION"],
            "count": 50,
            "get_cover_media_lists": True,
        }
        resp = await _graphql(
            "/graphql/query", DOC_ID_SAVED_COLLECTIONS,
            "PolarisProfileSavedTabContentQuery", variables)
        return N.norm_collections(resp)
    except IGError as e:
        return _fail(e)


@app.get("/saved/reels")
async def saved_reels(
    collection_id: str = Query(...),
    cursor: str = Query(""),
):
    # Reels in a saved collection. The "All posts" bucket has its own feed
    # endpoint; named collections use /feed/collection/{id}/posts/.
    try:
        if collection_id == ALL_SAVED_ID:
            path = "/api/v1/feed/saved/posts/"
            params = {"max_id": cursor} if cursor else None
            resp = await browser.ig_fetch(path, params=params)
        else:
            # Instagram's OWN web client always sends max_id on this endpoint,
            # even empty (?max_id=) - a bare request with no query string at
            # all 404s. browser.ig_fetch's generic param handling strips
            # empty-string values (fine for every other endpoint), so bake
            # the query string in directly here instead of via params=.
            # quote() the id: it lands in the PATH right before a
            # hand-built query string, so an unescaped ?/#/& in it would
            # retarget the upstream request.
            cid = quote(collection_id, safe="")
            path = (f"/api/v1/feed/collection/{cid}/posts/"
                    f"?{urlencode({'max_id': cursor})}")
            resp = await browser.ig_fetch(path)
        return N.norm_saved_feed(resp)
    except IGError as e:
        return _fail(e)


@app.get("/user")
async def user(
    username: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
):
    if not username and not user_id:
        raise HTTPException(422, "provide username or user_id")
    try:
        if username:
            resp = await browser.ig_fetch(
                "/api/v1/users/web_profile_info/",
                params={"username": username.lstrip("@")},
            )
            return N.norm_user(N.user_from_web_profile(resp))
        resp = await browser.ig_fetch(
            f"/api/v1/users/{user_id}/info/")
        return N.norm_user(resp.get("user"))
    except IGError as e:
        return _fail(e)


@app.get("/feed")
async def feed(
    amount: int = Query(12, ge=1, le=50),
    cursor: str = Query(""),
):
    try:
        form = {
            "reason": "pagination" if cursor else "cold_start_fetch",
            "is_pull_to_refresh": "0",
        }
        if cursor:
            form["max_id"] = cursor
        resp = await browser.ig_fetch(
            "/api/v1/feed/timeline/", method="POST", form=form)
        return N.norm_timeline_page(resp)
    except IGError as e:
        return _fail(e)


@app.get("/user/reels")
async def user_reels(
    username: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    amount: int = Query(24, ge=1, le=50),
    cursor: str = Query(""),
):
    try:
        pk = user_id or await _resolve_pk(username or "")
        form = {
            "target_user_id": pk,
            "page_size": str(amount),
            "include_feed_video": "true",
        }
        if cursor:
            form["max_id"] = cursor
        # clips/user gives reels + pagination. Instagram sometimes throttles
        # it to a login wall under heavy use; that clears on its own. We
        # surface the error rather than navigating (which crashes the
        # Playwright Firefox driver).
        resp = await browser.ig_fetch(
            "/api/v1/clips/user/", method="POST", form=form)
        return N.norm_clips_page(resp)
    except IGError as e:
        return _fail(e)


@app.get("/media")
async def media(
    code: Optional[str] = Query(None),
    pk: Optional[str] = Query(None),
):
    if not code and not pk:
        raise HTTPException(422, "provide code or pk")
    try:
        media_pk = pk or str(shortcode_to_pk(code))
        resp = await browser.ig_fetch(f"/api/v1/media/{media_pk}/info/")
        return N.norm_media_info(resp)
    except IGError as e:
        return _fail(e)


@app.get("/media/comments")
async def media_comments(
    media_id: str = Query(...),
    amount: int = Query(20, ge=1, le=50),
    cursor: str = Query(""),
):
    try:
        pk = pk_str(media_id)
        params = {"can_support_threading": "true"}
        if cursor:
            params["min_id"] = cursor
        resp = await browser.ig_fetch(
            f"/api/v1/media/{pk}/comments/", params=params)
        return N.norm_comments_page(resp)
    except IGError as e:
        return _fail(e)


@app.get("/media/comments/replies")
async def media_comment_replies(
    media_id: str = Query(...),
    comment_id: str = Query(...),
    amount: int = Query(20, ge=1, le=50),
    cursor: str = Query(""),
):
    try:
        pk = pk_str(media_id)
        params = {}
        if cursor:
            params["min_id"] = cursor
        resp = await browser.ig_fetch(
            f"/api/v1/media/{pk}/comments/{comment_id}/child_comments/",
            params=params)
        return N.norm_child_comments_page(resp)
    except IGError as e:
        return _fail(e)
