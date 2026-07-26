#!/usr/bin/env python3
"""Generate a deployable plugin from environment variables, so the backend
URL / API key / allowed hosts aren't hardcoded per deployment.

A Grayjay plugin is static client-side JS (it can't read env at runtime), so we
bake the values in here at "build" time and emit a ready-to-serve ./dist.

Env:
    IG_API_BASE    required   backend base URL, e.g.
                              https://ig.example.com  or  http://1.2.3.4:8000
    IG_API_KEY     optional   shared secret (must equal the server's IG_API_KEY)
    IG_SOURCE_URL  optional   where you serve the config (sets sourceUrl, used
                              by Grayjay for plugin auto-updates)

Writes ./dist/{InstagramScript.js, InstagramConfig.json, icon.png}. Serve
./dist and load <host>/InstagramConfig.json in Grayjay.

    IG_API_BASE=https://ig.example.com IG_API_KEY=... python3 configure.py
"""

import json
import os
import re
import shutil
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
api_base = (os.getenv("IG_API_BASE") or "").rstrip("/")
api_key = os.getenv("IG_API_KEY") or ""
source_url = os.getenv("IG_SOURCE_URL") or ""

if not api_base:
    sys.exit("IG_API_BASE is required "
             "(e.g. https://ig.example.com or http://1.2.3.4:8000)")
parsed = urlparse(api_base)
if not parsed.scheme or not parsed.hostname:
    sys.exit(f"IG_API_BASE is not a valid URL: {api_base!r}")

out = os.path.join(HERE, "dist")
os.makedirs(out, exist_ok=True)

# --- InstagramScript.js: swap the API_BASE / API_KEY constants ---------------
script = open(os.path.join(HERE, "InstagramScript.js")).read()
script, n1 = re.subn(r'let API_BASE = "[^"]*";',
                     f'let API_BASE = "{api_base}";', script, count=1)
script, n2 = re.subn(r'let API_KEY = "[^"]*";',
                     f'let API_KEY = "{api_key}";', script, count=1)
if not (n1 and n2):
    sys.exit("could not find the API_BASE / API_KEY lines in InstagramScript.js")
with open(os.path.join(out, "InstagramScript.js"), "w") as f:
    f.write(script)

# --- InstagramConfig.json: fix allowUrls (backend host) + sourceUrl ----------
cfg = json.load(open(os.path.join(HERE, "InstagramConfig.json")))
cdn = [x for x in cfg.get("allowUrls", [])
       if any(s in x for s in ("instagram.com", "cdninstagram", "fbcdn"))]
# host:port AND bare host (Grayjay matches both forms); CDN entries preserved.
cfg["allowUrls"] = list(dict.fromkeys([parsed.netloc, parsed.hostname] + cdn))
if source_url:
    cfg["sourceUrl"] = source_url
with open(os.path.join(out, "InstagramConfig.json"), "w") as f:
    json.dump(cfg, f, indent=4)

# --- copy the icon alongside (iconUrl is relative) ---------------------------
icon = os.path.join(HERE, "icon.png")
if os.path.exists(icon):
    shutil.copy(icon, os.path.join(out, "icon.png"))

print(f"wrote dist/ for backend {api_base} "
      f"(API key {'set' if api_key else 'EMPTY'})")
print(f"  allowUrls = {cfg['allowUrls']}")
if not source_url:
    print("  note: sourceUrl left as-is (set IG_SOURCE_URL to change it)")
