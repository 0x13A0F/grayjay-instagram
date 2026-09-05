# Screen recordings used in the README

The root [README](../../README.md) references four recordings. Until you drop
them in here, those images will show as broken on GitHub — record them before
publishing the repo.

| File | Slot | What to show |
|---|---|---|
| `demo.gif` | hero, top of the README | 6–10s: scrolling the home feed in Grayjay and opening a Reel that starts playing. This is the one people judge the project on. |
| `home-feed.gif` | Preview table | 4–6s: the home feed loading and scrolling. |
| `playlists.gif` | Preview table | 4–6s: Grayjay's *Playlists* showing your Instagram saved collections, opening one. |
| `login.gif` | Preview table | 6–8s: tapping **Login** on the source, the noVNC screen appearing, and it closing itself once the session lands. |

Blur or crop anything you don't want public — usernames you follow, your own
handle, DMs. The login recording in particular: **stop recording before you
type a password**, or cut that part out.

## Recording

Android: `adb shell screenrecord --time-limit 10 /sdcard/demo.mp4`, then
`adb pull /sdcard/demo.mp4`. Or any screen recorder app.

## Converting to a GIF

Two-pass with a generated palette — a single pass produces banded, muddy
colour:

```bash
ffmpeg -i demo.mp4 -vf "fps=15,scale=360:-1:flags=lanczos,palettegen" -y palette.png
ffmpeg -i demo.mp4 -i palette.png \
  -lavfi "fps=15,scale=360:-1:flags=lanczos[x];[x][1:v]paletteuse" \
  -y demo.gif
```

Trim first if needed: `-ss 00:00:02 -t 8` before `-i`. Keep each file under a
few MB — GitHub serves them on every page view. If one is stubborn, drop to
`fps=12` or `scale=300:-1` rather than shortening the clip.
