"""Pinned Camoufox fingerprint.

By default Camoufox rolls a fresh fingerprint on every launch. Instagram binds
a session to the fingerprint it first saw at login, so login and serving (and
every restart) must reuse the SAME one, or the session gets flagged.

Strategy: on the first launch we capture ONE real fingerprint preset (the
"real fingerprint presets" bundle added in Camoufox 152) and persist it as JSON
inside the mounted profile volume. Every later launch loads that exact preset
and passes it as `fingerprint_preset=`, so the identity is stable forever.

The pinned file lives next to the session (the profile dir is the mounted,
persisted volume). Delete it to re-roll a new fingerprint on the next login
(you'll need to log in again).
"""

import json
import os

from camoufox.fingerprints import get_random_preset
from camoufox.pkgman import installed_verstr

PROFILE_DIR = os.getenv("CAMOUFOX_PROFILE_DIR", "/data/profile")

# Persisted with the session so it survives restarts.
FINGERPRINT_FILE = os.getenv(
    "CAMOUFOX_FINGERPRINT_FILE",
    os.path.join(PROFILE_DIR, "camoufox-fingerprint.json"),
)

# OS family the pinned fingerprint should present as (macos | windows | linux).
# Camoufox spoofs this at the engine level regardless of the real host OS.
FINGERPRINT_OS = os.getenv("CAMOUFOX_FP_OS", "macos")

# Fallback UA, used ONLY if this Camoufox build ships no preset bundle. Keep it
# matched to the pinned browser (see the Dockerfile / README).
FALLBACK_UA = os.getenv(
    "CAMOUFOX_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) "
    "Gecko/20100101 Firefox/152.0",
)


def _ff_major() -> str:
    """Installed Camoufox Firefox major (e.g. '152'); selects the preset bundle."""
    try:
        return installed_verstr().split(".", 1)[0]
    except Exception:
        return ""


def load_or_create_preset():
    """Return the pinned fingerprint preset, creating + saving it on first use.

    Returns None if this Camoufox build bundles no presets (caller falls back).
    """
    try:
        with open(FINGERPRINT_FILE) as f:
            preset = json.load(f)
        print(f"fingerprint: using pinned preset from {FINGERPRINT_FILE}")
        return preset
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        print(f"fingerprint: could not read {FINGERPRINT_FILE} ({e}); re-rolling")

    preset = get_random_preset(os=FINGERPRINT_OS, ff_version=_ff_major())
    if not preset:
        print("fingerprint: no presets bundled in this Camoufox; using fallback UA")
        return None

    # Atomic write so a crash mid-write can't leave a half-file.
    os.makedirs(os.path.dirname(FINGERPRINT_FILE) or ".", exist_ok=True)
    tmp = FINGERPRINT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(preset, f)
    os.replace(tmp, FINGERPRINT_FILE)
    print(f"fingerprint: pinned a new preset -> {FINGERPRINT_FILE}")
    return preset


def fingerprint_kwargs() -> dict:
    """Launch kwargs that pin the identity; spread into AsyncCamoufox(**...)."""
    preset = load_or_create_preset()
    if preset is not None:
        return {"fingerprint_preset": preset}
    # Degraded path (no preset bundle): at least pin the user-agent.
    return {"config": {"navigator.userAgent": FALLBACK_UA}}
