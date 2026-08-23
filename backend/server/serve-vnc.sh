#!/bin/sh
# Backend entrypoint with an INTEGRATED, browser-based login.
#
# Starts a virtual X display (:0), a window manager, a VNC server, and noVNC
# (a browser-based VNC client on :6080), then runs the API. The API launches
# ONE headful Camoufox on :0. If the mounted profile has no Instagram session
# yet, the browser sits on the IG login page: open noVNC, log in, and the API
# auto-detects the session and starts serving from the SAME browser - no shell,
# no ENTER, no profile transfer. The same noVNC lets you re-login later.
#
# The plugin's "Login" button in Grayjay opens vnc-login/login.html (served
# here alongside noVNC), so the same login also works from the phone with no
# laptop involved - see plugin/configure.py (authentication.loginUrl).
#
# noVNC drives your logged-in account, so protect :6080 (set VNC_PASS, and/or
# firewall it). See README.
set -e
export DISPLAY=:0

# Virtual display + a minimal window manager (so dialogs/focus behave).
# VNC_GEOMETRY: the remote screen. The default is landscape; a portrait size
# (e.g. 900x1500x24) is far easier to use when logging in from a phone.
Xvfb :0 -screen 0 "${VNC_GEOMETRY:-1280x900x24}" >/tmp/xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &

if [ -n "$VNC_PASS" ]; then
    AUTH="-passwd $VNC_PASS"
    echo "VNC: password auth enabled."
else
    AUTH="-nopw"
    echo "VNC: NO password (set VNC_PASS to protect :6080)."
fi
x11vnc -display :0 -forever -shared $AUTH -rfbport 5900 >/tmp/x11vnc.log 2>&1 &

# Login wrapper pages, served from noVNC's own web root so they're same-origin
# with the VNC websocket: login.html embeds the client and, once the API
# reports a session, jumps to login-done.html (Grayjay's completionUrl).
cp /app/vnc-login/*.html /usr/share/novnc/

# noVNC web client -> websocket -> x11vnc (log in from a browser).
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
echo "noVNC ready on :6080  (open http://<host>:6080/vnc.html to log in)"

exec uvicorn app:app --host 0.0.0.0 --port 8000
