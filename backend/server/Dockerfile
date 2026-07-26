# Camoufox Instagram backend (headless, virtual display via xvfb).
FROM python:3.12-slim-bookworm

# Firefox/Camoufox runtime libs + xvfb (for headless="virtual").
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb ca-certificates fonts-liberation \
        libgtk-3-0 libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libdbus-glib-1-2 libxt6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download a pinned Camoufox browser into the image at build time. Pin a
# specific build so login and serving use the SAME Firefox (no profile /
# useragent mismatch) and the build is reproducible. `sync` refreshes the
# official release list into the cache first (required by the official
# package before a versioned `fetch`). The final line FAILS the build if the
# browser didn't actually install (e.g. a transient GitHub API rate-limit)
# instead of crashing at runtime.
#
# Firefox 152 -> keep the pinned UA in login.py and browser.py at Firefox/152.
# Bumping this build REQUIRES a re-login (the session is bound to the UA).
RUN python -m camoufox sync \
    && python -m camoufox fetch official/152.0.4-beta.28 \
    && python -c "from camoufox.pkgman import installed_verstr; \
print('Camoufox installed:', installed_verstr())"

# VNC bits for the INTEGRATED browser login (see serve-vnc.sh): x11vnc + a WM,
# plus noVNC + websockify so you log in from a browser. Separate layer (after
# the heavy camoufox fetch) so it stays cached.
RUN apt-get update && apt-get install -y --no-install-recommends \
        x11vnc fluxbox novnc python3-websockify \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

# Headful on the :0 xvfb (started by serve-vnc.sh) so the login is viewable
# over noVNC. Set CAMOUFOX_HEADLESS=virtual to run non-viewable instead.
ENV CAMOUFOX_PROFILE_DIR=/data/profile \
    CAMOUFOX_HEADLESS=false \
    DISPLAY=:0

EXPOSE 8000 6080
CMD ["sh", "/app/serve-vnc.sh"]
