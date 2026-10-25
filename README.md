# webradio_sportschau

Listen to the ARD **Sportschau Bundesliga radio commentary** from the terminal,
and sync it to a delayed TV picture.

`sportschau_stream.py` finds every audio stream of the current and next
matchday on sportschau.de, tells you which ones are live, and plays them
through [mpv](https://mpv.io/). It has a terminal UI and a small web page
for your phone, and a two-tap "mark/sync" that delays the radio until it
matches what you see on TV.

Single file, Python standard library only.

## Requirements

- Python 3.10+
- [mpv](https://mpv.io/) for playback (`brew install mpv` on macOS). Listing
  streams works without it.
- macOS (tested) or Linux (untested). Not Windows: the terminal UI uses
  `termios` and mpv control uses a Unix socket.

## Usage

```sh
python3 sportschau_stream.py                  # list all streams of this/next matchday
python3 sportschau_stream.py --live           # only streams whose feed is up now
python3 sportschau_stream.py --json           # same list as JSON
python3 sportschau_stream.py --live --mpv     # print an mpv command for the first live stream
python3 sportschau_stream.py <match page URL> # resolve a single sportschau.de match page

python3 sportschau_stream.py --ui             # interactive terminal player
python3 sportschau_stream.py --serve          # phone remote on http://<your-ip>:8790/
python3 sportschau_stream.py --ui --serve     # both, controlling the same mpv
```

In the list, `LIVE` means the broadcast has started and `pre` means the feed
is already up before the broadcast (often music, well before kickoff).
`--no-verify` skips checking the CDN and goes by the schedule only.

### Terminal UI keys

| Key | Action |
| --- | --- |
| `1`–`9` … `Enter` | play the stream with that number on screen |
| `space` | pause / resume |
| `,` `.` / `<` `>` / `←` `→` / `[` `]` / `{` `}` | seek ±1 s / ±5 s / ±10 s / ±1 min / ±5 min |
| `l` | jump to the live edge |
| `m` | mark / sync (see below) |
| `M` | clear mark and sync |
| `↑` `↓` | volume |
| `n` `p` | next / previous stream whose feed is up |
| `Tab` | show / hide the stream list |
| `a` | show all streams / only the relevant ones |
| `r` | refresh the stream list now |
| `s` | stop |
| `d` | detach: quit the UI, keep mpv playing (run `--ui` again to reattach) |
| `q` | quit and stop mpv |

If exactly one stream is live at start, the UI plays it right away.

**Stream list.** By default the player lists only streams whose feed is up
plus today's broadcasts that aren't over yet; finished matches and other
matchdays are hidden (`a` shows them). With `--live` it starts with only the
streams that are up. While something plays, the list collapses to one line
with the current match; `Tab` opens it, stopping opens it too. The list
refreshes itself every 2½ minutes, so `pre` turns into `LIVE` on its own.

**Reattach.** After `d`, running `--ui` (or `--serve`) again picks up the
stream mpv is playing, with its rewind buffer intact. The sync value is not
carried over; the audio delay itself is, since mpv keeps its position.

### Syncing the radio to the TV picture

TV (especially streaming) usually runs well behind the radio. To line them up:

1. Press `m` when you *hear* a moment: a goal, the whistle, a corner.
2. Press `m` again when you *see* that moment on TV.

The audio jumps back to the marked point and now runs behind by exactly the
measured gap, shown as `sync -N s`. If it is still ahead, repeat `m` … `m`
with the next moment: each round adds to the delay already set. Fine-tune with
`,` `.` (±1 s) and `<` `>` (±5 s); the shown value follows. Seeking, pausing
or switching streams between the two presses cancels the mark. Jumping to
live (`l`), switching streams, stopping or `M` resets the sync.

### Phone remote (`--serve`)

`--serve [PORT]` (default 8790) starts a small web page with the same
controls: play, pause, seek, live, mark/sync, volume and the stream list
(collapsed while playing, tap the bar to open it; "all" shows every stream). It
listens on all interfaces, so open `http://<your-mac-ip>:8790/` on your
phone. The address is printed at startup.

There is **no authentication**: anyone on your network can control playback.
It is meant for a home LAN only, so don't expose it to the internet. Requests
from other websites (cross-origin POSTs and DNS rebinding) are refused, and
the page can only play streams from the fetched list, not arbitrary URLs.

## How it works

The overview page
[Alle Audiostreams der Fußball-Bundesliga](https://www.sportschau.de/fussball/bundesliga/alle-audiostreams-der-fussball-bundesliga,audiostreams-bundesliga-uebersicht-100.html)
embeds the complete ARD player config for every stream as JSON in
`data-v="…"` attributes. Each entry contains the HLS and Icecast MP3 URLs,
the title, the broadcast start and the kickoff time. No API and no
JavaScript execution are needed. A stream is checked against the CDN (a
cheap request for the HLS master playlist) only when its broadcast start is
close enough to be plausibly live.

The script starts one background mpv with `--idle` and controls it over
mpv's JSON IPC socket. Every instance shares that mpv, so you can detach
and reattach, and run the terminal UI and the web UI side by side.

## Limitations

- **Rewind** works within what mpv has buffered since you started playing
  (up to about 15 minutes or 300 MiB), not into the server's 2-hour DVR
  window. To be able to rewind the whole match, start playing before kickoff.
- **1. Bundesliga only.** No 2. Bundesliga overview page has been found yet.
- The script depends on the layout of the sportschau.de overview page. If it
  finds nothing, check that page in a browser and update `OVERVIEW_URLS`.
- The player config marks the streams as geo-blocked. In practice the CDN
  has served them outside Germany, but that may change.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SPORTSCHAU_MPV_SOCKET` | `/tmp/sportschau-mpv.sock` | mpv IPC socket; set a different path to run a second, independent player |
| `SPORTSCHAU_MPV_LOG` | unset | write a verbose mpv log to this file for debugging |

## Tests

```sh
python3 -m unittest -v
```

The tests run offline. They exercise the finder against saved pages in
`fixtures/`, the timing and status logic, and the web server's request
guards.

## Disclaimer

Unofficial personal project, not affiliated with ARD, WDR or sportschau.de.
It only finds the public stream URLs that the sportschau.de player itself
uses. All content belongs to its respective owners.
