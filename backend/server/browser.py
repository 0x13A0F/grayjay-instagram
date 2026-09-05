"""Camoufox browser manager.

Holds ONE long-lived, logged-in Camoufox page and exposes `ig_fetch`,
which runs an in-page `fetch()` against Instagram from the real browser
origin (www.instagram.com) so requests carry the genuine session,
cookies and fingerprint. All access is serialized with a single lock
because a Playwright page is not safe for concurrent use.

Everything targets the SAME origin (www.instagram.com/api/v1/...) to
avoid CORS; that is also exactly what Instagram's own web app does.
"""

import asyncio
import json
import os
from typing import Any, Dict, Optional

from camoufox.async_api import AsyncCamoufox

import docids
from fingerprint import fingerprint_kwargs
from throttle import RateLimited, throttle
from utils import clip_text

IG_DOMAIN = "https://www.instagram.com"
IG_APP_ID = "936619743392459"
# The two paths Instagram posts persisted GraphQL queries to. We watch them
# during doc_id discovery to learn the ids its own JS is using - matched
# exactly, and only on the IG_DOMAIN origin.
GRAPHQL_PATHS = ("/api/graphql", "/graphql/query")
PROFILE_DIR = os.getenv("CAMOUFOX_PROFILE_DIR", "/data/profile")


def _parse_headless(v: str):
    """CAMOUFOX_HEADLESS -> playwright headless value.

    'virtual' = Camoufox's own xvfb (not viewable). 'false'/'0'/'' = headful on
    $DISPLAY (viewable via x11vnc/noVNC - needed for the integrated login).
    'true'/'1' = plain headless.
    """
    v = (v or "").strip().lower()
    if v == "virtual":
        return "virtual"
    if v in ("1", "true", "yes", "on"):
        return True
    return False


HEADLESS = _parse_headless(os.getenv("CAMOUFOX_HEADLESS", "virtual"))

_DEAD_MARKERS = (
    "handler is closed",   # the crash we actually hit (WriteUnixTransport)
    "has been closed",     # covers "target/context/browser has been closed"
    "target closed",
    "connection closed",
    "transport closed",
    "browser closed",
    "websocket",
)

# In-page fetch. Reads csrftoken from cookies and returns {status, body}
# so Python can decide how to parse (JSON vs an HTML login wall).
FETCH_JS_CODE = """
async (req) => {
    const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
    let claim = '0';
    try { claim = sessionStorage.getItem('www-claim-v2') || '0'; } catch (e) {}
    const headers = Object.assign({
        'x-ig-app-id': req.appId,
        'x-csrftoken': csrf,
        'x-asbd-id': '129477',
        'x-ig-www-claim': claim,
        'x-requested-with': 'XMLHttpRequest',
        'Accept': '*/*',
    }, req.headers || {});
    const opts = { method: req.method, headers, credentials: 'include' };
    if (req.body) opts.body = req.body;
    let status = 0, body = '';
    try {
        const r = await fetch(req.url, opts);
        status = r.status;
        body = await r.text();
    } catch (e) {
        status = -1;
        body = String(e);
    }
    return { status, body };
}
"""


# Pull fb_dtsg + lsd out of the page (IG embeds them in the bootstrap JSON).
TOKENS_JS_CODE = r"""
() => {
    const html = document.documentElement.innerHTML;
    const pick = (re) => { const m = html.match(re); return m ? m[1] : ""; };
    let dtsg = pick(/"DTSGInitData",\[\],\{"token":"([^"]+)"/)
        || pick(/"dtsg":\{"token":"([^"]+)"/);
    if (!dtsg) {
        const i = document.querySelector('input[name="fb_dtsg"]');
        if (i) dtsg = i.value;
    }
    let lsd = pick(/\["LSD",\[\],\{"token":"([^"]+)"/);
    if (!lsd) {
        const i = document.querySelector('input[name="lsd"]');
        if (i) lsd = i.value;
    }
    return { dtsg, lsd };
}
"""


def camoufox_extra_args():
    # Pin the persisted fingerprint so serving matches the login session.
    return fingerprint_kwargs()


class IGBrowser:
    def __init__(self):
        self.camoufox = None
        self.browser = None
        self.page = None
        self.lock = asyncio.Lock()
        self.ready = False
        # True when there's no valid IG session: the user must log in via
        # noVNC. A background task watches for the login and flips ready on.
        self.needs_login = False
        self._login_task = None
        # Set to a dict while discover_doc_ids() is running; the request
        # listener only harvests then, so our OWN /api/graphql calls (which
        # carry a possibly-stale id) can never overwrite a learned one.
        self._harvest = None

    async def start(self):
        self.camoufox = AsyncCamoufox(
            headless=HEADLESS,
            persistent_context=True,
            user_data_dir=PROFILE_DIR,
            **camoufox_extra_args(),
        )
        self.browser = await self.camoufox.__aenter__()
        # persistent_context: pages live on the context/browser object.
        if getattr(self.browser, "pages", None):
            self.page = self.browser.pages[0]
        else:
            self.page = await self.browser.new_page()
        self.page.set_default_timeout(45000)
        self.page.on("request", self._on_request)
        await self._goto_ig()
        # Integrated login: if the profile has no session yet, don't block
        # startup - drop to the IG login page (viewable over noVNC) and poll
        # for the session in the background. Serving stays 503 until logged in.
        if await self._has_session():
            self.ready = True
            self.needs_login = False
            print("Instagram session found - serving.")
        else:
            self.ready = False
            self.needs_login = True
            print("NO Instagram session - open noVNC (:6080 /vnc.html) "
                  "and log in; serving resumes automatically.")
            try:
                await self.page.goto(
                    IG_DOMAIN + "/accounts/login/",
                    wait_until="domcontentloaded")
            except Exception as e:
                print(f"login-page nav failed (ignored): {e}")
            self._ensure_login_watch()

    async def _goto_ig(self):
        last_err = None
        for attempt in range(5):
            try:
                await self.page.goto(
                    IG_DOMAIN + "/", wait_until="domcontentloaded")
                return
            except Exception as e:
                last_err = e
                print(f"startup goto failed (try {attempt + 1}): {e}")
                await asyncio.sleep(3)
        raise last_err

    async def _has_session(self) -> bool:
        """Is there a live instagram.com sessionid cookie?"""
        if self.page is None:
            return False
        try:
            cookies = await self.page.context.cookies()
        except Exception:
            return False
        return any(
            c.get("name") == "sessionid"
            and "instagram.com" in (c.get("domain") or "")
            and c.get("value")
            for c in cookies
        )

    async def viewer_id(self) -> str:
        """The logged-in user's own pk, read from the ds_user_id cookie
        (Instagram sets it on login). Empty string if not logged in. Used to
        build /friendships/{id}/following/ for subscription import."""
        if self.page is None:
            return ""
        async with self.lock:
            try:
                cookies = await self.page.context.cookies()
            except Exception:
                return ""
        for c in cookies:
            if (c.get("name") == "ds_user_id"
                    and "instagram.com" in (c.get("domain") or "")):
                return c.get("value") or ""
        return ""

    def _ensure_login_watch(self):
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.create_task(self._watch_login())

    async def _watch_login(self):
        """Poll until the user logs in via noVNC, then resume serving."""
        while self.needs_login:
            await asyncio.sleep(3)
            if self.page is None:
                return
            async with self.lock:
                try:
                    if await self._has_session():
                        # Normalize back to the home origin for in-page fetch.
                        await self.page.goto(
                            IG_DOMAIN + "/", wait_until="domcontentloaded")
                        self.needs_login = False
                        self.ready = True
                        print("Login detected - serving resumed.")
                        return
                except Exception as e:
                    print(f"login watch error (ignored): {e}")

    def _mark_login_needed(self):
        if not self.needs_login:
            print("Session looks logged out - open noVNC to re-login.")
        self.needs_login = True
        self.ready = False
        self._ensure_login_watch()

    async def _maybe_flag_logout(self):
        """Flag a re-login only if the sessionid cookie is actually gone -
        so a transient rate-limit login wall isn't mistaken for a logout."""
        async with self.lock:
            has = await self._has_session()
        if not has:
            self._mark_login_needed()

    async def stop(self):
        self.ready = False
        try:
            if self.camoufox is not None:
                await self.camoufox.__aexit__(None, None, None)
        except Exception as e:
            # The browser may already be dead; elaunch cleanly.
            print(f"browser stop error (ignored): {e}")
        finally:
            self.camoufox = None
            self.browser = None
            self.page = None

    async def restart(self):
        print("browser crashed - relaunching Camoufox...")
        await self.stop()
        await self.start()
        print("browser relaunched.")

    def _on_request(self, request) -> None:
        """Harvest doc_ids from Instagram's own GraphQL posts.

        Sync Playwright callback: never raise, never block. Inert unless
        discover_doc_ids() has opened a harvest window.
        """
        if self._harvest is None:
            return
        try:
            if request.method != "POST":
                return
            # Origin-check, not a substring match: any frame on the page can
            # post to its own /api/graphql, and we must not learn ITS ids.
            from urllib.parse import urlsplit
            u = urlsplit(request.url)
            if f"{u.scheme}://{u.netloc}" != IG_DOMAIN:
                return
            if u.path not in GRAPHQL_PATHS:
                return
            body = request.post_data or ""
            if "doc_id" not in body:
                return
            from urllib.parse import parse_qs
            q = parse_qs(body)
            friendly = (q.get("fb_api_req_friendly_name") or [""])[0]
            doc_id = (q.get("doc_id") or [""])[0]
            if friendly and doc_id:
                self._harvest[friendly] = doc_id
                docids.observe(friendly, doc_id)
        except Exception as e:
            print(f"doc_id harvest error (ignored): {e}")

    async def discover_doc_ids(self, query: str = "reels") -> Dict[str, str]:
        """Load a real search SERP and learn which doc_ids Instagram uses.

        This is an ordinary page load on the logged-in account - the same
        thing a person searching would do - but it IS account traffic, so
        callers must respect docids.DISCOVERY_COOLDOWN. Scrolling the SERP
        triggers the separate pagination query, whose id we also need.
        Returns {friendly_name: doc_id} for everything seen.
        """
        if not self.ready or self.page is None:
            return {}
        from urllib.parse import quote
        seen: Dict[str, str] = {}
        async with self.lock:
            self._harvest = seen
            try:
                await throttle.wait()
                await self.page.goto(
                    f"{IG_DOMAIN}/explore/search/keyword/?q={quote(query)}",
                    wait_until="domcontentloaded")
                await asyncio.sleep(4)
                for _ in range(3):
                    if docids.SEARCH_PAGE in seen:
                        break
                    await self.page.evaluate(
                        "() => window.scrollBy(0, window.innerHeight * 3)")
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"doc_id discovery failed: {e}")
            finally:
                self._harvest = None
                # ig_fetch runs in-page, so leave the page back on the home
                # origin regardless of how discovery went.
                try:
                    await self.page.goto(
                        IG_DOMAIN + "/", wait_until="domcontentloaded")
                except Exception as e:
                    print(f"post-discovery nav failed (ignored): {e}")
        print(f"doc_id discovery saw {len(seen)} quer"
              f"{'y' if len(seen) == 1 else 'ies'}: {sorted(seen)}")
        return seen

    async def get_tokens(self) -> Dict:
        """Extract fb_dtsg + lsd from the logged-in page. Instagram's
        /api/graphql POSTs are rejected (HTML shell) without them."""
        if not self.ready or self.page is None:
            return {}
        async with self.lock:
            try:
                return await self.page.evaluate(TOKENS_JS_CODE)
            except Exception as e:
                print(f"get_tokens failed: {e}")
                return {}

    def _abs_url(self, path: str, params: Optional[Dict]) -> str:
        url = path if path.startswith("http") else IG_DOMAIN + path
        if params:
            from urllib.parse import urlencode
            clean = {k: v for k, v in params.items()
                     if v is not None and v != ""}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urlencode(clean)
        return url

    async def ig_fetch_raw(
        self,
        path: str,
        params: Optional[Dict] = None,
        method: str = "GET",
        form: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> Dict:
        """Run an in-page fetch; return {status, body} without raising."""
        if not self.ready or self.page is None:
            return {"status": 503, "body": "browser not ready"}

        body = None
        hdrs = dict(headers or {})
        if form is not None:
            from urllib.parse import urlencode
            body = urlencode(form)
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

        req = {
            "url": self._abs_url(path, params),
            "method": method,
            "headers": hdrs,
            "body": body,
            "appId": IG_APP_ID,
        }

        async with self.lock:
            # Pace INSIDE the lock so the gap is measured between actual
            # Instagram calls. Cache hits never get here, so repeat browsing
            # stays instant; only real outbound traffic is spaced.
            from throttle import requested_interval
            try:
                await throttle.wait(requested_interval.get())
            except RateLimited as e:
                # We're in a backoff window. Answer immediately instead of
                # holding the request (and this lock) open for minutes.
                raise IGError(
                    429,
                    f"backing off after a rate-limit; retry in "
                    f"{e.retry_after:.0f}s",
                    retry_after=e.retry_after) from None
            try:
                result = await self.page.evaluate(FETCH_JS_CODE, req)
            except Exception as e:
                # If the browser/page transport died, relaunch once and retry.
                # Anything else (a real page-level error) propagates.
                if not _is_browser_dead(e):
                    raise
                await self.restart()
                result = await self.page.evaluate(FETCH_JS_CODE, req)
        return result

    async def ig_fetch(
        self,
        path: str,
        params: Optional[Dict] = None,
        method: str = "GET",
        form: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> Any:
        """Run an in-page fetch and return parsed JSON.

        Raises IGError on non-2xx or non-JSON (e.g. login wall).
        """
        result = await self.ig_fetch_raw(
            path, params, method, form, headers)
        status = result.get("status", 0)
        text = result.get("body", "") or ""
        if status < 200 or status >= 300:
            if status == 401:
                await self._maybe_flag_logout()
            if status == 429:
                throttle.penalize()
            raise IGError(status, clip_text(text))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Almost always an HTML login/checkpoint page - if the session
            # cookie is gone, switch to "needs login" so noVNC re-login works.
            await self._maybe_flag_logout()
            # A wall served to a LIVE session is a soft rate-limit, so widen
            # the gap - but a GraphQL rejection (retired doc_id) is not about
            # request volume, and backing off 60s for it would just make the
            # relearn-and-retry crawl.
            if not _is_query_rejection(text):
                throttle.penalize()
            raise IGError(status, "non-JSON response: " + clip_text(text))
        throttle.relax()
        return parsed


class IGError(Exception):
    def __init__(self, status: int, detail: str, retry_after: float = 0.0):
        super().__init__(f"IG {status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def _is_query_rejection(text: str) -> bool:
    """Facebook's `for (;;);{"__ar":1,"error":...}` envelope.

    The anti-JSON-hijacking prefix marks a response from the GraphQL/AJAX
    tier - i.e. the request reached Instagram and was rejected on its
    merits (typically a retired doc_id), as opposed to a login/checkpoint
    wall, which is HTML.
    """
    return text.lstrip().startswith("for (;;);")


def _is_browser_dead(e: Exception) -> bool:
    s = str(e).lower()
    return any(m in s for m in _DEAD_MARKERS)


# Module-level singleton used by the FastAPI app.
browser = IGBrowser()
