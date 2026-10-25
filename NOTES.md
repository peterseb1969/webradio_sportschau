# NOTES

## 2026-09-13 — finder verified, mpv controller + terminal UI built

Live matches used for testing: RB Leipzig–Hamburger SV (liga1spiel8, 15:30),
SV Elversberg–Bayern (liga1spiel9, 17:30; feed was up with music from before 16:30).

### Finder
See the docstring in `sportschau_stream.py` and the Background section of the
brief. Everything comes from one overview page; the player config is embedded
as JSON in `data-v` attributes. Fixtures in `fixtures/`.

Correction to the brief: single-match broadcasts start 15 min before kickoff,
but the Konferenz starts 20 min before (seen in the overview fixture). The
live window is computed from the broadcast start, so nothing depends on it.

### Tests
`python3 -m unittest -v` from the repo root. Offline only: the finder against
`fixtures/`, timing/status logic, and the web server's request guards.

### mpv over JSON IPC
- Command used to start mpv:
  `mpv --idle=yes --terminal=no --really-quiet --input-ipc-server=/tmp/sportschau-mpv.sock <MPV_FLAGS>`
- Commands: `loadfile <url> replace`, `set_property force-media-title`,
  `set_property pause`, `cycle pause`, `seek <s> relative`, `add volume <d>`,
  `stop`, `quit`; properties read: `media-title idle-active pause
  paused-for-cache time-pos demuxer-cache-duration volume audio-bitrate`.
- **macOS Unix socket paths are capped at ~104 chars.** A socket under the
  long per-session temp dir silently never appeared; `/tmp/...` works.
- **DVR / rewind.** mpv starts a live HLS playlist at the live edge (per
  `EXT-X-START:TIME-OFFSET=-16`) and only sees ~16 s of history, so
  `seek -60` right after start clamps to 0. Rewinding works within what mpv
  has buffered since play started (`--demuxer-max-back-bytes=300MiB`,
  observed fine). Forcing `--demuxer-lavf-o=live_start_index=0` makes mpv
  start 2 h behind with `duration` growing as it downloads and no way to jump
  to live, so that option is not used. To have the full match rewindable,
  start playback at kickoff (brief task 5: auto-start).
- A second client can attach to the running mpv via the socket; `--ui` reuses
  an existing mpv instead of starting another. `d` in the UI detaches and
  leaves mpv playing.

### Mark / sync (m, M)
`m` stores the current audio position; a second `m` seeks back to it and
adds the audio played since the mark to the picture delay (shown as
`sync -N s`). Repeated rounds add up (playback always did; the shown value
used to be replaced by the last round, fixed 2026-10-25). Seeks adjust the
value; live edge, stream switch, stop and `M` clear it.
Any seek, pause, stream switch or stop between the presses drops the mark. The seek checks mpv's `demuxer-cache-state`
seekable ranges first and refuses if the mark has left the buffer.

### LAN web UI (`--serve [PORT]`, default 8790)
`http.server` + `ThreadingHTTPServer`, one inline HTML page, polls `/status`
every second. Endpoints: `GET /status`, `POST /play {i}`, `/pause`, `/seek {s}`,
`/live`, `/volume {d}`, `/stop`, `/mark`, `/clear`, `/refresh`, `/next {step}`.
All go through the shared `Session` (RLock-serialised; mpv's socket is not
thread-safe). `--ui --serve` runs both front ends on the same session.
- Port 8765 was taken by another local service; macOS lets `0.0.0.0:PORT`
  bind next to someone else's `127.0.0.1:PORT`, so the script probes the
  port first and refuses if busy.
- SIGTERM is handled like Ctrl-C (mpv is stopped, socket removed).
- **One mpv per socket.** Every instance shares `/tmp/sportschau-mpv.sock`;
  quitting one instance with `q` stops that mpv for the others too (they
  recover by starting a new one, but playback is interrupted). Set
  `SPORTSCHAU_MPV_SOCKET=/tmp/other.sock` for an independent player.
- `SPORTSCHAU_MPV_LOG=/tmp/mpv.log` writes a verbose mpv log for debugging.

### How to run
```
python3 sportschau_stream.py            # list
python3 sportschau_stream.py --ui       # interactive player
python3 sportschau_stream.py --serve    # phone remote at http://<mac-ip>:8790/
python3 sportschau_stream.py --ui --serve   # both
python3 sportschau_stream.py --live --mpv   # print an mpv command
```

## Open questions
- Is there a 2. Bundesliga overview page? Obvious URLs 404; the 2. BL section
  showed no audiostream teasers on 2026-09-13.
- How early exactly does a slot's feed go up, and does it carry the previous
  match on a reused slot (e.g. liga1spiel8 Sun 15:30 then Sun 17:30 next week)?
- Does `isGeoBlocked: true` ever get enforced by the CDN?
