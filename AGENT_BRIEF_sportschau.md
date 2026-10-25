# Mission: Sportschau Bundesliga audio — stream finder + mpv front end

You are working in a directory containing `sportschau_stream.py`, a stdlib-only
Python CLI that lists ARD Sportschau Bundesliga audio streams. You have full
internet access, and `mpv` (0.41), `ffmpeg` and `ffprobe` are installed on
this Mac. The goal is a small, reliable tool that (a) lists the currently live
audio streams, (b) opens a selected stream in mpv, and (c) lets the user
control playback — first from the terminal, later from a phone on the LAN.

The end user is one person listening at home. Nothing here is served to the
public.

## Status

- **Finder: DONE and verified live on 2026-09-13** (see Background). Do not
  redo it. `sportschau_stream.py` lists all streams, marks live ones, and
  prints an mpv command. `fixtures/` holds saved HTML from that day for
  offline tests. The old heuristic version (`sportschau_stream.v1.py`) was
  removed on 2026-10-25; it is in the initial commit if ever needed.
- **Controller + terminal UI: DONE 2026-09-13** (`Mpv` class, `run_ui()`,
  `--ui`), tested live against Leipzig–HSV. See NOTES.md for mpv findings.
- **LAN UI: DONE 2026-09-13** (`--serve`, `Session` shared with the terminal
  UI, inline HTML page, mark/sync as a big button). `--ui --serve` runs both.

## Architecture (decided — build to this)

Three separate, independently testable parts, all in `sportschau_stream.py`
(or a small package if it grows past ~600 lines):

1. **Finder** — `find_streams()` / `resolve_page()` / `extract_streams()`.
   Returns records:
   `{"title", "slot", "hls", "mp3", "page", "start", "kickoff",
     "dvr_seconds", "geo_flag", "scheduled_live", "live", "status"}`
   where `status` is `"live"` (broadcast running), `"pre"` (feed up before
   broadcast start) or `""`.
2. **Controller** — starts mpv with `--input-ipc-server=<socket>` and talks
   JSON IPC over that Unix socket: `loadfile`, `cycle pause`, `seek`,
   `set volume`, `quit`, and reads properties (`time-pos`, `demuxer-cache-duration`,
   `pause`, `media-title`). mpv does all audio work. The controller never
   touches audio data.
3. **Front ends** over the controller:
   - `--ui`: terminal menu (numbered list of streams, live ones first, then a
     key loop: space pause, ←/→ seek ±10 s, ↑/↓ volume, `n`/`p` next/previous
     stream, `r` re-run finder, `q` quit). Plain `sys.stdin` + `termios` raw
     mode or `curses`, your choice; no third-party packages.
   - `--serve [port]`: the same controls as a single-page HTML UI served by
     `http.server`, bound to the LAN address so a phone can control playback
     while audio plays on the Mac. JSON endpoints: `GET /streams`,
     `POST /play`, `POST /pause`, `POST /seek`, `POST /volume`, `POST /stop`,
     `GET /status`. The HTML is inline in the Python file.

Keep the existing CLI surface working: `url` positional, `--live`, `--json`,
`--mpv`, `--mp3`, `--no-verify`, `--ui`, `--serve [PORT]`; exit codes 0 found / 1 nothing / 2 usage.
Keep `MPV_FLAGS` (the 15-min cache / seekable settings) as the default
launch flags.

## Background (verified 2026-09-13 during a live match day — trust this)

**Where the list lives.** One page lists every audiostream of the current
and next matchday (12 entries observed: 11 matches + Konferenz):
`https://www.sportschau.de/fussball/bundesliga/alle-audiostreams-der-fussball-bundesliga,audiostreams-bundesliga-uebersicht-100.html`
No 2. Bundesliga equivalent was found (the obvious URLs 404). `OVERVIEW_URLS`
in the script is a list so more can be added.

**How stream URLs are embedded.** The overview page and every per-match
audiostream page (and its `~player.html` variant) embed the full ARD player
config as HTML-escaped JSON in `data-v="..."` on
`<div data-v-type="MediaPlayer">`. No API call, no JS execution. Fields used:
```
mc.streams[].media[].url            HLS master + Icecast MP3
mc.meta.title                       "Live hören: RB Leipzig gegen Hamburger SV - Bundesliga"
mc.meta.broadcastedOnDateTime       broadcast start (UTC), 15 min before kickoff
mc.meta.synopsis                    "3. Spieltag: Anpfiff des Spiels ist am 13.09.2026 um 15:30 Uhr. ..."
mc.live.dvrWindowSeconds            7200
mc.live.availFromDateTime           = broadcast start
mc.isGeoBlocked                     true (but see geo note)
mc.pluginData["sharing@web"].link   canonical match page URL
```

**Stream URL anatomy.** Slot channels are fixed and reused every matchday
(`liga1spiel1..9`, `liga1konferenz` observed; the same slot serves e.g. Leipzig–HSV
on Sunday and Schalke–Elversberg the next Sunday):
```
HLS  https://sportschau-radio.ard-mcdn.de/sportschau/radio/<slot>/hls/master.m3u8
     -> 96/seglist_<a>_<b>.m3u8 and 192/seglist_<a>_<b>.m3u8  (AAC-LC 48 kHz stereo)
     4 s segments (#EXT-X-TARGETDURATION:4), EXT-X-PROGRAM-DATE-TIME present,
     ~1876 segments ≈ 2 h 05 retained (matches dvrWindowSeconds 7200),
     LL-HLS advertised (CAN-SKIP-UNTIL=24, _HLS_skip) but optional; mpv plays
     the master directly. seglist ids change per session; always start from
     master.m3u8. `192/master.m3u8` does not exist (404).
MP3  https://wdr-sportschau-<slot>.icecastssl.wdr.de/wdr/sportschau/<slot>/mp3/high?file=.mp3&aggregator=web
     -> 302 to https://f<nnn>.rndfnk.com/ard/wdr/sportschau/<slot>/mp3/128/stream.mp3?...token=...
     128 kbps MP3, tokenised per request, no rewind. Never GET it to "verify" —
     it streams forever (this hung the old script).
```

**Timing.** The CDN feed is up well before the broadcast: at 16:30 CEST the
17:30 match's HLS already had a 2 h window. The finder therefore checks the
CDN for anything from 3 h before broadcast start to 2 h 15 after, and marks
feed-up-before-start as `pre`. Match pages self-expire ~90 min after full time.

**Geo.** `isGeoBlocked` is true in the config, yet the CDN served the stream
to a Swiss residential connection. Treat the flag as advisory.

**Decoding.** `ffprobe` on the master: AAC, 48 kHz, 2 channels. mpv 0.41
plays it with the default `MPV_FLAGS`.

## Tasks, in order

1. ~~Make the finder real.~~ DONE. Only touch it if the site contradicts
   the background; then fix and note it in NOTES.md.

2. ~~Build the controller.~~ DONE. Spec kept for reference: no live match needed: test against any public
   HLS radio stream (e.g. a Deutschlandfunk or SRF HLS URL). Requirements:
   - start mpv detached with `--input-ipc-server`, `--idle=yes`, plus
     `MPV_FLAGS`; wait for the socket to appear;
   - reconnect to an already running mpv if the socket exists and answers;
   - a `send(cmd: list) -> dict` with request ids and a short timeout;
   - convenience methods: `play(url, title)`, `toggle_pause()`, `seek(secs)`,
     `volume(delta)`, `status()`, `stop()`, `quit()`;
   - clean shutdown: `quit` on exit, remove the socket file.

3. ~~Terminal UI (`--ui`).~~ DONE. Glue finder + controller. Show the list,
   pick by number, then the key loop above with a one-line status
   (stream title, paused/playing, time-pos, cached seconds, volume) that
   refreshes ~1×/s. This is the first version the user will actually use.

4. ~~LAN web UI (`--serve`).~~ DONE. Thin HTTP layer over the same controller:
   one HTML page with the stream list (refresh button) and play/pause/seek/
   volume/stop buttons, polling `/status` every 2 s. Bind to `0.0.0.0` only
   when `--serve` is given; print the URL with the Mac's LAN IP at startup.
   No auth needed (home LAN), but say so in the docstring.

5. **Nice to have, do last.** Kickoff countdown for upcoming streams in both
   UIs; auto-start mpv at broadcast start for a chosen match so the full DVR
   window is in mpv's cache; look for a 2. Bundesliga overview page.

6. **Test and document.** Run `--ui` and `--serve` against a live match;
   paste real output into `NOTES.md`. Outside a match window, test the
   controller and both UIs with a public HLS stream and mark the live parts
   as "needs live window" with the exact commands.

## Deliverables

- Updated `sportschau_stream.py` (stdlib only, working, tested as far as the
  match calendar allows).
- `NOTES.md`: the mpv IPC command set used, how to run each mode, anything
  that contradicted the background, and open questions only more match days
  can answer.

## Constraints

- Read-only use of a free, unauthenticated, non-DRM public service. No login
  areas, no DRM circumvention, no paywalled content, no re-streaming to
  anyone else.
- Be polite: ≤ ~1 request/second sustained, no bulk downloading, identify
  with a normal browser UA. The finder makes one page request plus one small
  playlist request per plausibly-live stream; keep it that way.
- The script stays stdlib-only. Diagnostic tools (`ffprobe`, `curl`) are fine
  for *your* investigation.
- No headless-browser automation.
- If the site structure contradicts the background above, the site wins —
  update NOTES.md accordingly.
