# Grayjay Instagram plugin

A [Grayjay](https://grayjay.app) source for **Instagram** — a **home feed**
(your timeline), search creators, browse & play their **Reels**, subscribe to
accounts, and read **comments (with replies)** — without the end-user logging in.

It's two pieces:

- **`plugin/`** — the Grayjay plugin (a thin JavaScript client).
- **`backend/server/`** — a self-hosted backend that drives a real,
  logged-in **[Camoufox](https://camoufox.com)** (stealth Firefox) browser
  and exposes a small REST API. The browser profile *is* the session, so the
  plugin needs no tokens — you log in once with your own account, through a
  browser-based login UI (noVNC) baked into the backend.

```
[Grayjay plugin] ──HTTP──> [Camoufox backend] ──logged-in browser──> [Instagram]
```

Why a real browser: Instagram aggressively flags the private mobile API.
Driving an actual logged-in browser carries a genuine session/fingerprint and
calls the same `/api/v1` endpoints the web app uses (via an in-page `fetch`).

## Setup

**One command, no manual login step.** Copy [.env.example](.env.example) to
`.env` (repo root) and fill it in, then:
```bash
docker compose up -d --build
```
This builds and runs **both** pieces — the backend (`:8000`, plus a **noVNC**
login UI on `:6080`) and the plugin server (`:8080`). Open
`http://<host>:6080/vnc.html` once to log in to Instagram (the backend
auto-detects the session and starts serving); then open `http://<host>:8080/`
and **scan the QR code** in Grayjay (or load `InstagramConfig.json` from that
same URL directly):

<img src="plugin/qrcode_example.png" alt="Plugin install page with a scannable QR code" width="320">

Full details — including deploying on [Dokploy](https://dokploy.com) straight
from git (no shell access needed) — in
[backend/server/README.md](backend/server/README.md).

## Scope
- **Home feed** — your logged-in timeline (videos only; photos/carousels are
  filtered out since the plugin plays video).
- **Search** — keyword **reel search** (Grayjay's search bar) and **creator
  search**; plus **channel browse** + subscribe (Grayjay-local).
- **Reels/videos** playback.
- **Comments**, including **replies** — reply *previews* come free; full reply
  threads are fetched on demand behind the **"Load comment replies"** setting.
- **Import subscriptions** — in Grayjay, open the Instagram source's detail
  page and tap **Login** once (a one-tap formality — see below), then
  **Import Subscriptions** pulls in the accounts you follow as Grayjay
  subscriptions.

Posting/following on Instagram's side are out of scope.

## Layout
```
plugin/          Grayjay plugin (InstagramConfig.json + InstagramScript.js)
backend/server/  Camoufox + FastAPI backend (app.py, browser.py, …)
```

## Roadmap
Planned next: **importing** your playlists and **playlist search**.
(Subscription import and server-side response caching are done.)

> Heads-up: it runs on a single Instagram account. Heavy, rapid browsing can
> temporarily rate-limit some endpoints (e.g. the reels feed); they recover
> with a short rest.
