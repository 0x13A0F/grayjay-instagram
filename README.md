# Grayjay Instagram plugin

A [Grayjay](https://grayjay.app) source for **Instagram** — a **home feed**
(your timeline), search creators, browse & play their **Reels**, open your
**saved collections** as playlists, and read **comments (with replies)** —
with no Instagram credentials ever entered into Grayjay itself.

It's two pieces:

- **`plugin/`** — the Grayjay plugin (a thin JavaScript client).
- **`backend/server/`** — a self-hosted backend that drives a real,
  logged-in **[Camoufox](https://camoufox.com)** (stealth Firefox) browser
  and exposes a small REST API. The browser profile *is* the session, so the
  plugin needs no tokens — you log in once with your own account, through a
  browser-based login UI (noVNC) baked into the backend and reachable from
  Grayjay itself.

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
`http://<host>:8080/` and **scan the QR code** in Grayjay (or load
`InstagramConfig.json` from that same URL directly):

<img src="plugin/qrcode_example.png" alt="Plugin install page with a scannable QR code" width="320">

Then log in to Instagram **once**, either way:

- **Phone only** — tap **Login** on the Instagram source in Grayjay. Its login
  webview opens the backend's real browser over noVNC; log in there and the
  screen closes itself once the session lands. Nothing else is needed.
- **From a computer** — open `http://<host>:6080/vnc.html` and log in there.

Either way the backend auto-detects the session and starts serving.

Full details — including deploying on [Dokploy](https://dokploy.com) straight
from git (no shell access needed) — in
[backend/server/README.md](backend/server/README.md).

## Scope
- **Home feed** — your logged-in timeline (videos only; photos/carousels are
  filtered out since the plugin plays video).
- **Search** — keyword **reel search** (Grayjay's search bar) and **creator
  search**; plus **channel browse** + subscribe (Grayjay-local).
- **Reels/videos** playback.
- **Saved playlists** — your Instagram **saved collections** show up in
  Grayjay's *Playlists* search (plus an "All saved reels" entry) and play like
  any other playlist.
- **Comments**, including **replies** — reply *previews* come free; full reply
  threads are fetched on demand behind the **"Load comment replies"** setting.
- **Response cache** — repeated requests (flipping between pages, re-opening a
  channel) are served from Redis instead of hitting Instagram again, which
  keeps navigation snappy and rate-limits away. The **"Cache duration"**
  plugin setting controls it (Off / 1 / 3 / 5 / 10 minutes).
- **Request pacing** — the backend enforces a minimum gap between outbound
  Instagram calls (with jitter), and backs off automatically when Instagram
  pushes back. Instagram flags accounts on request *bursts*, so this is
  mandatory, not optional: the **"Request spacing"** plugin setting (Fast 1s /
  Normal 2s / Careful 4s / Very careful 8s) can only ask the backend to go
  *slower* than its own floor.
- **Import subscriptions** — in Grayjay, open the Instagram source's detail
  page and tap **Login** (that's the noVNC login above, opened in Grayjay's
  own webview), then **Import Subscriptions** pulls in the accounts you follow
  as Grayjay subscriptions.

Posting/following on Instagram's side are out of scope.

## Layout
```
plugin/          Grayjay plugin (InstagramConfig.json + InstagramScript.js)
backend/server/  Camoufox + FastAPI backend (app.py, browser.py, …)
```

## Roadmap
Planned next: **importing** your saved collections through Grayjay's *Import
Playlists* (browsing and searching them already works).

> Heads-up: it runs on a single Instagram account. Heavy, rapid browsing can
> temporarily rate-limit some endpoints (e.g. the reels feed); they recover
> with a short rest. If you see Instagram's "we suspect automated behaviour"
> notice, raise **"Request spacing"** and give the account a day of normal
> use.
