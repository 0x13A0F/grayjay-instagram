# Camoufox Instagram backend

A real-browser backend for the Grayjay Instagram plugin. It holds **one
long-lived, logged-in [Camoufox](https://camoufox.com)** page (stealth
Playwright-Firefox) and, for each request, runs an in-page `fetch()` against
Instagram's `/api/v1` endpoints — so calls carry a genuine session, cookies,
and fingerprint. The **browser profile is the session** (no tokens).
Serves on **:8000**.

```
plugin ──:8000──> FastAPI ──> one logged-in Camoufox page
                              └─ page.evaluate(fetch('/api/v1/...')) ──> Instagram
```

## Files
| File | Role |
|---|---|
| `app.py` | FastAPI routes (`/health`, `/search/users`, `/search/reels`, `/user`, `/feed`, `/following`, `/saved/collections`, `/saved/reels`, `/user/reels`, `/media`, `/media/comments`, `/media/comments/replies`, `/docids`) + the auth + response-cache middleware |
| `browser.py` | Camoufox manager + `ig_fetch()` (in-page fetch) + serialization lock + integrated login detection + self-heal on crash |
| `throttle.py` | mandatory outbound pacing — minimum gap + jitter between real Instagram calls, and automatic backoff after a 429 (see **Request pacing**) |
| `docids.py` | GraphQL persisted-query id registry — resolves each id and relearns it when Instagram rotates its web build (see **GraphQL doc_ids**) |
| `cache.py` | best-effort Redis response cache (see **Caching**) — key builder + safe get/set that never breaks a request |
| `normalize.py` | Instagram JSON → the shapes the plugin reads |
| `utils.py` | shared stateless helpers (incl. reel shortcode → media pk codec) |
| `fingerprint.py` | pins one real Camoufox fingerprint preset (persisted in the profile) so login + serving + restarts share the same identity |
| `serve-vnc.sh` | container entrypoint — starts the virtual display + noVNC, then the API (see **Login** below) |
| `vnc-login/` | the two pages Grayjay's **Login** button drives: `login.html` (noVNC + `/health` watcher) and `login-done.html` (the plugin's `completionUrl`) |
| `Dockerfile` | the backend image (xvfb + Firefox). The stack's `docker-compose.yml` lives at the **repo root** |

Tests live in **`backend/tests/`** (a sibling of `server/`) — run them from
`backend/` with `PYTHONPATH=server`: `test_normalize.py` / `test_shortcode.py` /
`test_docids.py` / `test_throttle.py` (unit tests, no browser, network or
Redis needed). The plugin has its own: `node plugin/test_pagers.js`.

## Setup

> **TL;DR:** set the `.env` (step 1) → `docker compose up -d --build`
> (step 2) → log in once, from a browser at `http://<host>:6080/vnc.html`
> or straight from the phone via Grayjay's **Login** button (see **Login**).

### 1. Configure (.env)
The compose stack lives at the **repo root** — run these steps from there.
Both services read a `.env` at the root (gitignored). For a public deployment:
```bash
cd <repo root>
cat > .env <<'EOF'
IG_API_KEY=<paste: openssl rand -hex 32>            # shared secret (auth)
IG_API_BASE=https://ig.example.com                  # PUBLIC backend URL
IG_SOURCE_URL=https://ig.example.com/InstagramConfig.json
EOF
```
- `IG_API_KEY` — the backend rejects any request (except `/health`) without a
  matching `X-API-Key`; the plugin sends it. **Unset = open mode** (private LAN
  only; the server logs a warning). 
- `IG_API_BASE` / `IG_SOURCE_URL` — the **public** URLs the plugin (running in
  Grayjay, not in Docker) uses to reach the backend / fetch updates. Defaults
  suit same-host local testing; set them to your LAN IP or VPS domain.

### 2. Run everything (one command)
```bash
docker compose up -d --build        # from the repo root
```
This starts **three** services:
- `ig-camoufox` — the backend on **:8000**, plus the **noVNC login UI** on
  **:6080** (build context `./backend/server`).
- `ig-plugin` — builds `./plugin`, runs `configure.py` (bakes the `.env` values
  into the plugin, incl. `allowUrls`) and http-serves it on **:8080**, incl. a
  QR install page.
- `redis` — the response cache (internal only; see **Caching** below).

Open `http://<host>:8080/` and **scan the QR code** in Grayjay (Settings →
Plugins → Add → Scan QR code) — or install directly from
`http://<host>:8080/InstagramConfig.json`.

<img src="../../plugin/qrcode_example.png" alt="Plugin install page with a scannable QR code" width="320">

```bash
curl http://localhost:8000/health
# {"status":"ok","ready":false,"needs_login":true}  -> log in (below)
```

## Login (integrated, no shell / no TTY)

The backend runs **one headful Camoufox** on a virtual display and serves it
over **noVNC**. On first run the profile has no session, so the browser sits on
the Instagram login page and `/health` reports `needs_login:true`. Just:

1. Open **`http://<host>:6080/vnc.html`** in any browser and click **Connect**.
2. Log in fully (email / 2FA) until you see your feed.
3. Done — the backend **auto-detects** the session (polls for the `sessionid`
   cookie), navigates home, and starts serving. `/health` flips to
   `ready:true`. Nothing to press; the **same** browser now serves requests.

### …or from the phone, inside Grayjay (no laptop)

Install the plugin first, then on the Instagram source's detail page tap
**Login**. Grayjay's login webview opens `login.html` on the noVNC port — the
same remote browser, wrapped in a page that watches `/health` and, the moment
the session lands, navigates to `login-done.html`. That URL is the plugin's
`completionUrl`, so Grayjay closes the webview and marks the source
**logged in** (which is also what reveals **Import Subscriptions**).

- Both pages live in [vnc-login/](vnc-login), copied next to noVNC's own web
  root at startup by `serve-vnc.sh` (so they share the VNC websocket origin).
- `IG_VNC_BASE` tells the plugin where that page is; it defaults to
  `IG_API_BASE`'s host on `:6080`. Set it if you moved the port, use a
  separate domain, or terminate TLS in front.
- Set **`VNC_GEOMETRY`** to a portrait size (e.g. `900x1500x24`) — the default
  `1280x900x24` is landscape and cramped on a phone.
- The **Skip** button in the corner completes the flow by hand, for when the
  backend can't be polled (e.g. the page is HTTPS but the API is HTTP).
- On an HTTPS deploy the noVNC port must be TLS-terminated too: an `https://`
  wrapper page cannot open a `ws://` VNC socket (mixed content).

The session + the pinned fingerprint are written into the **named Docker
volume** `ig-profile` (declared in `docker-compose.yml`), so they survive
container restarts, rebuilds, **and redeploys** - it's owned by Docker, not
the git checkout, so tools that `rm -rf` and re-clone the repo on every
deploy (Dokploy included) don't touch it. To **re-login** later (session
expired), open the same noVNC URL and log in again — the backend notices and
resumes. No profile transfer, no `docker compose run`, no ENTER.

> Only `docker compose down -v` (the `-v`) or `docker volume rm` deletes this
> volume - a plain redeploy or `down`/`up` never does. To force a fresh login,
> remove it deliberately: `docker compose down && docker volume rm
> <project>_ig-profile` (find the exact name with `docker volume ls`).

> **Protect :6080** — whoever opens it can drive your logged-in Instagram. Set
> `VNC_PASS` (adds a noVNC password) and/or firewall the port to your IP. On a
> public host also front it (and :8000) with HTTPS so the VNC stream and the API
> key aren't in clear text.

### Deploy on Dokploy (UI-only, from git)
1. **New → Compose**, point it at this repo; **Compose Path** = `docker-compose.yml` (repo root).
2. In **Environment**, set `IG_API_KEY`, `IG_API_BASE`, `IG_SOURCE_URL`,
   `VNC_PASS`, and — if `8000/8080/6080` are already used on the box — custom
   **host** ports `IG_API_PORT` / `IG_PLUGIN_PORT` / `IG_VNC_PORT`. Point
   `IG_API_BASE` / `IG_SOURCE_URL` at those same ports, e.g.:
   ```
   IG_API_PORT=18000
   IG_PLUGIN_PORT=18080
   IG_VNC_PORT=16080
   IG_API_BASE=http://<server-ip>:18000
   IG_SOURCE_URL=http://<server-ip>:18080/InstagramConfig.json
   ```
   (Only the host side moves; the containers still listen on 8000/8080/6080.)
   `IG_VNC_PORT` is also what the plugin's **Login** button points at — it's
   derived automatically, so `IG_VNC_BASE` only needs setting if noVNC lives
   somewhere else entirely (its own domain, or TLS in front).
3. **Deploy.** Dokploy runs `docker compose up -d --build` — no manual commands.
4. Open `http://<server-ip>:<IG_PLUGIN_PORT>/` and scan the QR code in Grayjay
   to install, then tap **Login** on the source and log in there — or, from a
   computer, open **`http://<server-ip>:<IG_VNC_PORT>/vnc.html`** instead
   (step above). The backend starts serving on its own either way.

Redeploys (git push → Redeploy) reuse the `ig-profile` volume, so you stay
logged in.

> **Cleaner alternative on a busy box:** skip host ports entirely and give each
> service a **domain** in Dokploy (Traefik routes by hostname, adds HTTPS, no
> port clashes). Add a basic-auth middleware to the noVNC one. Then the `*_PORT`
> vars don't matter — Traefik reaches the containers over the compose network.
> Set `IG_VNC_BASE` to the noVNC domain in that setup, and make sure Traefik
> passes **WebSocket upgrades** through to it: over HTTPS the login page can
> only reach the VNC socket as `wss://`.

## Signing the plugin (optional)

Without a signature, Grayjay installs the plugin fine but shows a **missing
signature** notice, since it can't verify who published it. Fixing that means
giving the plugin container an RSA private key at build time — the `plugin`
service signs the *actual deployed* script with it every time it starts
(RSA-SHA512, the same scheme as Grayjay's own `scriptSignature` /
`scriptPublicKey` fields), so signing has to happen after `IG_API_BASE`/
`IG_API_KEY` are baked in, not once in the repo. Generate a key once (any
RSA key works — a fresh one or an existing `~/.ssh/id_rsa`; both the
traditional PEM and modern OpenSSH private-key formats are supported):

```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa_grayjay -N ""   # keep the private key
```

Then give it to the container **either** way (only one is needed):

- **As an env var (`IG_SIGN_KEY_B64`)** — no volume mount, so it works from a
  UI-only host like Dokploy with no shell/file access:
  ```bash
  base64 -w0 ~/.ssh/id_rsa_grayjay          # Linux
  base64 -i ~/.ssh/id_rsa_grayjay | tr -d '\n'   # macOS (no -w flag)
  ```
  Paste the single-line output as `IG_SIGN_KEY_B64` in `.env` (or Dokploy's
  Environment box). Base64 avoids every multi-line-value pitfall (`.env`
  parsing, UI textareas, YAML, shell quoting all choke on a raw multi-line PEM).
- **As a mounted file (`IG_SIGN_KEY_PATH`)** — if you do have host/file
  access: uncomment the `volumes:` line under the `plugin` service in
  `docker-compose.yml` (mounts the key **read-only**), then set
  `IG_SIGN_KEY_PATH=/keys/id_rsa` in `.env` (the **in-container** path from
  that mount, not a path on your host).

Either way, rebuild (`docker compose up -d --build`). The private key never
leaves your server and isn't baked into the image; only set one of these if
you want the notice gone. Re-installing after re-signing (e.g. you rotated
the key) requires removing and re-adding the plugin in Grayjay, since it pins
the public key it first saw.

## Caching

The `redis` service caches successful responses so Grayjay's constant repeat
requests (flipping pages, re-opening a channel, re-running a search) are served
instantly instead of driving the browser again — smoother navigation and far
less Instagram rate-limiting.

- **Duration is set in the plugin**, not here: the **"Cache duration"** setting
  in Grayjay (Off / 1 / 3 / 5 / 10 min, default 3) is sent per request as an
  `X-Cache-TTL` header; the backend caches that response for that long. `Off`
  sends no header and nothing is cached.
- Only `200` JSON is cached (keyed by method + path + query, so each page /
  cursor is separate); errors and login walls never are.
- `CACHE_MAX_TTL` (default 600s) caps whatever the plugin asks for.
- **Fail-safe**: if Redis is down the backend just serves uncached — it never
  errors or slows down, and caching resumes on its own when Redis returns.
- Redis is internal (no published port), memory-capped (128 MB, LRU) and
  disposable (no volume) — it's only a cache.

Check it: responses carry an `X-Cache: HIT|MISS` header;
`docker exec ig-redis redis-cli keys 'igcache*'` lists cached entries.

## Request pacing

Instagram doesn't flag accounts on request *volume* so much as on request
*bursts* — a page arriving every 80ms is unmistakably not a person. Grayjay
will happily ask for pages as fast as they arrive, so the backend paces
outbound calls itself, in `throttle.py`:

- A **minimum gap** between two real Instagram calls, with **jitter** (±25%),
  because perfectly regular spacing is itself a signal.
- The gap is a **floor** (`IG_MIN_INTERVAL_FLOOR`, default 1s). The plugin
  asks for a gap via `X-Min-Interval` (the **"Request spacing"** setting), and
  that request is clamped: a client can only ever ask the backend to go
  *slower*, never faster.
- **Automatic backoff**: a 429 — or a login wall served to a still-valid
  session, which is a soft rate-limit — pauses Instagram calls for
  `IG_BACKOFF_BASE` (60s), doubling per consecutive hit up to
  `IG_BACKOFF_MAX` (15 min), and decaying as calls start succeeding again.
- Pacing is **outbound only**: cache hits never reach it, so repeat browsing
  stays instant.

- **Requests are never parked.** Pacing waits at most `IG_MAX_WAIT` (15s)
  inside a request; if the wait would be longer — which is exactly what a
  backoff window means — the backend answers `429` + `Retry-After`
  immediately. Holding the connection instead just turns into a client-side
  timeout (Grayjay reports a `408`) and blocks everything queued behind it.

`GET /throttle` reports the current state (`strikes`, `penalty_remaining`);
`POST /throttle/reset` clears a backoff window when you believe Instagram has
cooled off (normal pacing still applies — it doesn't make Instagram forget).
The plugin never retries a 429, and shows the remaining cooldown.

## GraphQL doc_ids

A few endpoints (keyword reel search, saved collections) aren't in Instagram's
REST API — they're **persisted GraphQL queries**. The web client never sends
query text; each query is registered on Meta's servers at build time and
addressed by a numeric `doc_id`. We impersonate that client, so we must send
the same ids.

The catch: **Instagram retires those ids on every web release**, and the id is
identical for every account on earth — so a rotation breaks every deployment of
this backend at the same moment. Symptom: `/search/reels` (or
`/saved/collections`) starts returning 500 with a non-JSON body like
`for (;;);{"__ar":1,"error":1357004,"errorSummary":"Sorry, something went
wrong"...}` — a generic rejection that looks nothing like "your id expired".

**This is handled automatically.** On such a failure the backend opens a real
search page in the browser it already drives, watches which `doc_id`
Instagram's own JavaScript posts, caches it in Redis (14 days) and retries the
call. A rotation costs one page load, not a new release.

- `GET /docids` — the ids in use and where each came from (`env` / `learned` /
  `builtin`).
- `POST /docids/refresh` — force a discovery pass (`?force=true` skips the
  cooldown). Rarely needed.
- Discovery is real traffic on your account, so it's rate-limited to one pass
  per `IG_DOCID_DISCOVERY_COOLDOWN` (default 900s) no matter how many requests
  fail.
- `IG_DOC_ID_SEARCH` / `IG_DOC_ID_SEARCH_PAGE` / `IG_DOC_ID_SAVED` pin an id by
  hand — an escape hatch if discovery can't find one (e.g. Instagram renamed
  the query). **Pinning disables relearning for that query.**

The built-in ids in `docids.py` are only the cold-start fallback, used until
discovery runs for the first time.

## Notes / limits
- **Heavy/slow**: a browser is hundreds of MB and seconds per call; requests
  are serialized (one page, one lock). Repeat requests are absorbed by the
  Redis cache (above).
- **Single account**: rapid browsing of many creators' reels can temporarily
  rate-limit the `clips` endpoint (login wall); it recovers on its own.
- **Pinned fingerprint**: the first login captures one real Camoufox
  fingerprint preset and saves it to `camoufox-fingerprint.json` **inside the
  profile dir**; login, serving, and every restart replay that exact
  fingerprint (via `fingerprint.py`), so Instagram sees a stable identity. The
  UA is part of that preset (auto-matched to the pinned Firefox build) — no
  separate UA to keep in sync. Delete the file to re-roll (requires a new
  login); `CAMOUFOX_FP_OS` (default `macos`) sets the OS family on first roll.
- **Self-healing**: if the browser/page transport dies, the next request
  relaunches Camoufox and retries once — a crash recovers instead of wedging.
- **`ig-profile` (Docker volume) holds your session and fingerprint.** Not a
  git-tracked path, so there's nothing to accidentally commit. To inspect or
  back it up: `docker run --rm -v <project>_ig-profile:/data -v "$PWD":/backup
  busybox tar czf /backup/ig-profile.tar.gz -C /data .`
