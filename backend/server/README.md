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
| `app.py` | FastAPI routes (`/health`, `/search/users`, `/search/reels`, `/user`, `/feed`, `/saved/collections`, `/saved/reels`, `/user/reels`, `/media`, `/media/comments`, `/media/comments/replies`) |
| `browser.py` | Camoufox manager + `ig_fetch()` (in-page fetch) + serialization lock + integrated login detection + self-heal on crash |
| `normalize.py` | Instagram JSON → the shapes the plugin reads |
| `utils.py` | shared stateless helpers (incl. reel shortcode → media pk codec) |
| `fingerprint.py` | pins one real Camoufox fingerprint preset (persisted in the profile) so login + serving + restarts share the same identity |
| `serve-vnc.sh` | container entrypoint — starts the virtual display + noVNC, then the API (see **Login** below) |
| `Dockerfile` | the backend image (xvfb + Firefox). The stack's `docker-compose.yml` lives at the **repo root** |

Tests live in **`backend/tests/`** (a sibling of `server/`) — run them from
`backend/` with `PYTHONPATH=server`: `test_normalize.py` / `test_shortcode.py`
(unit tests, no browser needed).

## Setup

> **TL;DR:** set the `.env` (step 1) → `docker compose up -d --build`
> (step 2) → open `http://<host>:6080/vnc.html` and log in once (see **Login**).

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
This starts **two** services:
- `ig-camoufox` — the backend on **:8000**, plus the **noVNC login UI** on
  **:6080** (build context `./backend/server`).
- `ig-plugin` — builds `./plugin`, runs `configure.py` (bakes the `.env` values
  into the plugin, incl. `allowUrls`) and http-serves it on **:8080**.

Install the plugin in Grayjay from `http://<host>:8080/InstagramConfig.json`.
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

The session + the pinned fingerprint are written into the mounted `ig-profile/`
volume, so they survive restarts. To **re-login** later (session expired), open
the same noVNC URL and log in again — the backend notices and resumes. No
profile transfer, no `docker compose run`, no ENTER.

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
3. **Deploy.** Dokploy runs `docker compose up -d --build` — no manual commands.
4. Open **`http://<server-ip>:<IG_VNC_PORT>/vnc.html`** and log in once (step
   above). The backend serves automatically; add the plugin in Grayjay from
   `IG_SOURCE_URL`.

Redeploys (git push → Redeploy) reuse the `ig-profile` volume, so you stay
logged in.

> **Cleaner alternative on a busy box:** skip host ports entirely and give each
> service a **domain** in Dokploy (Traefik routes by hostname, adds HTTPS, no
> port clashes). Add a basic-auth middleware to the noVNC one. Then the `*_PORT`
> vars don't matter — Traefik reaches the containers over the compose network.

## Notes / limits
- **Heavy/slow**: a browser is hundreds of MB and seconds per call; requests
  are serialized (one page, one lock). No response caching yet (planned).
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
- **`ig-profile/` holds your session — gitignored. Never commit it.**
