#!/usr/bin/env python3
"""Generate a deployable plugin from environment variables, so the backend
URL / API key / allowed hosts aren't hardcoded per deployment.

A Grayjay plugin is static client-side JS (it can't read env at runtime), so we
bake the values in here at "build" time and emit a ready-to-serve ./dist.

Env:
    IG_API_BASE     required   backend base URL, e.g.
                               https://ig.example.com  or  http://1.2.3.4:8000
    IG_API_KEY      optional   shared secret (must equal the server's IG_API_KEY)
    IG_SOURCE_URL   optional   where you serve the config (sets sourceUrl, used
                               by Grayjay for plugin auto-updates)
    IG_VNC_BASE     optional   PUBLIC base URL of the backend's noVNC login UI,
                               e.g. http://1.2.3.4:6080. Grayjay's "Login"
                               button opens it, so the first-time Instagram
                               login works from the phone. Defaults to
                               IG_API_BASE's host on port IG_VNC_PORT.
    IG_VNC_PORT     optional   host port the noVNC UI is published on
                               (default 6080). Only used to build the
                               IG_VNC_BASE default; ignored if that is set.
    IG_SIGN_KEY_B64         optional   a PEM RSA private key, base64-encoded to
                                       one line (e.g. `base64 -w0 key.pem` or,
                                       on macOS, `base64 -i key.pem | tr -d
                                       '\\n'`) - pastes into a UI env var field
                                       with no volume mount needed. Takes
                                       priority over IG_SIGN_KEY_PATH.
    IG_SIGN_KEY_PATH        optional   path to a PEM RSA private key (mounted
                                       into the container) to sign the script
                                       with instead of IG_SIGN_KEY_B64.
    IG_SIGN_KEY_PASSPHRASE  optional   passphrase for that key, if encrypted

    Signing is optional either way. Without it, Grayjay installs the plugin
    fine but shows a "missing signature" security notice.

Writes ./dist/{InstagramScript.js, InstagramConfig.json, icon.png, qr.svg,
index.html}. Serve ./dist - load <host>/InstagramConfig.json in Grayjay
directly, or open <host>/ for a page with a QR code to scan instead.

Grayjay's native "Login" button (Source Detail screen) is pointed at the
BACKEND's noVNC login wrapper, not at a page served from here: tapping it
opens the real Camoufox browser sitting on Instagram's login page, and the
wrapper jumps to completionUrl once the backend reports a live session -
which closes the webview and marks the source logged in (that's what unlocks
Import Subscriptions/Playlists). So the whole first-time setup is doable from
the phone alone.

    IG_API_BASE=https://ig.example.com IG_API_KEY=... python3 configure.py
"""

import base64
import html
import json
import os
import re
import shutil
import sys
from urllib.parse import urlencode, urlparse

import qrcode
import qrcode.image.svg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

HERE = os.path.dirname(os.path.abspath(__file__))
api_base = (os.getenv("IG_API_BASE") or "").rstrip("/")
api_key = os.getenv("IG_API_KEY") or ""
source_url = os.getenv("IG_SOURCE_URL") or ""
vnc_base = (os.getenv("IG_VNC_BASE") or "").rstrip("/")
vnc_port = (os.getenv("IG_VNC_PORT") or "").strip() or "6080"

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

# --- optional: sign the DEPLOYED script (RSA-SHA512, PKCS1v15) ---------------
# Must sign the script as actually served (API_BASE/API_KEY already baked in
# above), not the source template - Grayjay verifies scriptSignature against
# the bytes it downloads from scriptUrl. Same scheme as Grayjay's own
# sign-script.sh: PKCS8 SubjectPublicKeyInfo public key, PEM header/footer and
# newlines stripped to one line; base64 signature, also one line.
sign_key_b64 = os.getenv("IG_SIGN_KEY_B64") or ""
sign_key_path = os.getenv("IG_SIGN_KEY_PATH") or ""
sign_key_passphrase = os.getenv("IG_SIGN_KEY_PASSPHRASE") or None
script_signature = ""
script_public_key = ""
private_key_pem = None
sign_error = None
if sign_key_b64:
    try:
        private_key_pem = base64.b64decode(sign_key_b64, validate=True)
    except Exception as e:
        sign_error = f"IG_SIGN_KEY_B64 is not valid base64: {e}"
elif sign_key_path:
    try:
        with open(sign_key_path, "rb") as f:
            private_key_pem = f.read()
    except OSError as e:
        sign_error = f"could not read IG_SIGN_KEY_PATH ({sign_key_path}): {e}"

if sign_error:
    print(f"WARNING: {sign_error}")
    print("  Continuing WITHOUT a signature (Grayjay will show its "
          "missing-signature notice).")
elif private_key_pem is not None:
    sign_password = sign_key_passphrase.encode() if sign_key_passphrase else None
    try:
        try:
            # Traditional PEM (PKCS1/PKCS8) - what `ssh-keygen -m PEM` writes.
            private_key = serialization.load_pem_private_key(
                private_key_pem, password=sign_password)
        except ValueError:
            # Modern ssh-keygen's OWN default format ("-----BEGIN OPENSSH
            # PRIVATE KEY-----") - what you get if -m PEM was left off, incl.
            # most pre-existing ~/.ssh/id_rsa keys. Different loader, same key.
            private_key = serialization.load_ssh_private_key(
                private_key_pem, password=sign_password)
        signature = private_key.sign(
            script.encode("utf-8"), padding.PKCS1v15(), hashes.SHA512())
        script_signature = base64.b64encode(signature).decode()
        pub_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        script_public_key = "".join(pub_pem.strip().splitlines()[1:-1])
    except Exception as e:
        # Never let a bad/misconfigured signing key take the whole plugin
        # server down - signing is optional; fall back to unsigned and keep
        # serving. The docker-compose logs get a clear reason why.
        print(f"WARNING: could not sign the script with the given key: {e}")
        print("  Continuing WITHOUT a signature (Grayjay will show its "
              "missing-signature notice). Check IG_SIGN_KEY_B64/PATH and "
              "IG_SIGN_KEY_PASSPHRASE.")

# --- InstagramConfig.json: fix allowUrls (backend host) + sourceUrl ----------
cfg = json.load(open(os.path.join(HERE, "InstagramConfig.json")))
cdn = [x for x in cfg.get("allowUrls", [])
       if any(s in x for s in ("instagram.com", "cdninstagram", "fbcdn"))]
# host:port AND bare host (Grayjay matches both forms); CDN entries preserved.
cfg["allowUrls"] = list(dict.fromkeys([parsed.netloc, parsed.hostname] + cdn))
if source_url:
    cfg["sourceUrl"] = source_url
# authentication: point Grayjay's built-in login webview at the backend's
# noVNC login wrapper, so the Instagram login itself happens inside Grayjay
# (phone-only setup). The wrapper needs the API origin to poll /health, hence
# the ?api= parameter.
#
# completionUrl ends with "?*", which Grayjay matches as "same URL, ignoring
# the query string" - so the wrapper can add a cache-buster and still trigger
# onLogin (exact-match would be brittle). Reaching it closes the webview and
# flips the source to logged-in, which is what reveals Import Subscriptions.
if not vnc_base:
    # Same host as the API, on the port noVNC is PUBLISHED on - that's the
    # host side of the compose port mapping (IG_VNC_PORT), not the fixed 6080
    # inside the container.
    vnc_base = f"{parsed.scheme}://{parsed.hostname}:{vnc_port}"
vnc_parsed = urlparse(vnc_base)
if not vnc_parsed.scheme or not vnc_parsed.hostname:
    sys.exit(f"IG_VNC_BASE is not a valid URL: {vnc_base!r}")
# The login webview lives on the noVNC origin, so allow it too (it's usually
# the same host as the API, just another port).
cfg["allowUrls"] = list(dict.fromkeys(
    cfg["allowUrls"] + [vnc_parsed.netloc, vnc_parsed.hostname]))
login_url = f"{vnc_base}/login.html?{urlencode({'api': api_base})}"
cfg["authentication"] = {
    "loginUrl": login_url,
    "completionUrl": f"{vnc_base}/login-done.html?*",
}
if script_signature:
    cfg["scriptSignature"] = script_signature
    cfg["scriptPublicKey"] = script_public_key
else:
    cfg.pop("scriptSignature", None)
    cfg.pop("scriptPublicKey", None)
with open(os.path.join(out, "InstagramConfig.json"), "w") as f:
    json.dump(cfg, f, indent=4)

# --- copy the icon alongside (iconUrl is relative) ---------------------------
icon = os.path.join(HERE, "icon.png")
if os.path.exists(icon):
    shutil.copy(icon, os.path.join(out, "icon.png"))

# --- install page: QR code (scan in Grayjay) + the same URL as text ----------
# Grayjay's scanner/deep-link handler wants its own "grayjay://plugin/<url>"
# scheme, not the bare config URL - it rejects a plain http(s) QR with
# "Not a plugin url". The visible/copyable text field still shows the plain
# URL, for pasting into Grayjay's "Add source" field by hand.
install_url = cfg["sourceUrl"]
grayjay_url = f"grayjay://plugin/{install_url}"
qr_img = qrcode.make(grayjay_url, image_factory=qrcode.image.svg.SvgPathImage)
qr_img.save(os.path.join(out, "qr.svg"))

warning_html = ""
if not source_url:
    warning_html = (
        '<div class="warning">IG_SOURCE_URL was not set at build time - this '
        "QR code points at a placeholder URL, not your server. Set "
        "IG_SOURCE_URL and rebuild.</div>"
    )
page = open(os.path.join(HERE, "index.html")).read()
page = page.replace("{{SOURCE_URL}}", html.escape(install_url))
page = page.replace("{{GRAYJAY_URL}}", html.escape(grayjay_url))
page = page.replace("{{WARNING_HTML}}", warning_html)
with open(os.path.join(out, "index.html"), "w") as f:
    f.write(page)

print(f"wrote dist/ for backend {api_base} "
      f"(API key {'set' if api_key else 'EMPTY'})")
print(f"  allowUrls = {cfg['allowUrls']}")
print(f"  install page + QR -> {grayjay_url}")
print(f"  authentication.loginUrl -> {login_url}")
print(f"  authentication.completionUrl -> {cfg['authentication']['completionUrl']}")
if script_signature:
    print("  script signed (scriptSignature/scriptPublicKey set)")
else:
    print("  script NOT signed - Grayjay will show a missing-signature notice. "
          "Set IG_SIGN_KEY_B64 (or IG_SIGN_KEY_PATH) to sign it.")
if not source_url:
    print("  note: sourceUrl left as-is (set IG_SOURCE_URL to change it)")
