#!/usr/bin/env python3
"""
sportschau_stream.py — find ARD Sportschau Bundesliga audio streams.

How it works (verified 2026-09-13 during a live match day):

  * sportschau.de keeps an overview page listing every audiostream of the
    current and next matchday:
      https://www.sportschau.de/fussball/bundesliga/alle-audiostreams-der-fussball-bundesliga,audiostreams-bundesliga-uebersicht-100.html
  * That page (and every per-match audiostream page) embeds the complete
    ARD player config as JSON in a `data-v="..."` attribute of a
    <div data-v-type="MediaPlayer">.  The interesting part is `mc`:
      mc.streams[].media[].url      -> stream URLs (HLS master + Icecast MP3)
      mc.meta.title                 -> "Live hören: RB Leipzig gegen Hamburger SV - Bundesliga"
      mc.meta.broadcastedOnDateTime -> start of the broadcast (UTC), 15 min before kickoff
      mc.meta.synopsis              -> "3. Spieltag: Anpfiff des Spiels ist am 13.09.2026 um 15:30 Uhr. ..."
      mc.live.dvrWindowSeconds      -> 7200 (server-side rewind window)
      mc.live.availFromDateTime     -> same as broadcast start
      mc.pluginData["sharing@web"].link -> canonical match page URL
    No separate API call and no JavaScript execution are needed.
  * Stream URL anatomy (slot channels are fixed, reused every matchday):
      HLS:  https://sportschau-radio.ard-mcdn.de/sportschau/radio/<slot>/hls/master.m3u8
            renditions 96/ and 192/ kbps AAC, 4 s segments, ~2 h DVR window,
            LL-HLS advertised but optional
      MP3:  https://wdr-sportschau-<slot>.icecastssl.wdr.de/wdr/sportschau/<slot>/mp3/high?file=.mp3&aggregator=web
            302 -> tokenised 128 kbps Icecast stream, no rewind
      <slot> = liga1spiel1..9, liga1konferenz (others may appear)
    `isGeoBlocked` is true in the config, but the CDN served the stream to a
    Swiss residential connection without complaint.

Usage:
  python3 sportschau_stream.py                 # list all known streams; LIVE = broadcast running,
                                               # pre = feed already up before broadcast start
  python3 sportschau_stream.py --json          # same as JSON
  python3 sportschau_stream.py --live          # only streams that are live right now
  python3 sportschau_stream.py --ui            # interactive player: pick a stream, control mpv,
                                               # m/m to sync the audio to a delayed TV picture
  python3 sportschau_stream.py --serve         # LAN web UI on http://<mac-ip>:8790/ (phone remote)
  python3 sportschau_stream.py --ui --serve    # both at once, same mpv
  python3 sportschau_stream.py --mpv           # print an mpv command for the first live stream
  python3 sportschau_stream.py <match page URL> # resolve one match page

--ui starts one background mpv (--idle, --input-ipc-server=/tmp/sportschau-mpv.sock)
and drives it over JSON IPC.  mpv starts at the live edge; rewinding works
within what mpv has buffered since you pressed play (see MPV_FLAGS), not into
the server-side 2 h DVR window (mpv/ffmpeg cannot jump around in a live HLS
playlist without starting 2 h behind).

Stdlib only, no dependencies.
"""

import argparse
import datetime as dt
import html as htmllib
import ipaddress
import json
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OVERVIEW_URLS = [
    "https://www.sportschau.de/fussball/bundesliga/"
    "alle-audiostreams-der-fussball-bundesliga,audiostreams-bundesliga-uebersicht-100.html",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MPV_FLAGS = ("--no-video --cache=yes --cache-secs=900 --demuxer-max-bytes=300MiB "
             "--demuxer-max-back-bytes=300MiB --force-seekable=yes")
# Unix socket paths are capped at ~104 chars on macOS: keep this short.
# Every instance of this script shares one mpv through this socket; override
# with SPORTSCHAU_MPV_SOCKET to run a second, independent player.
MPV_SOCKET = os.environ.get("SPORTSCHAU_MPV_SOCKET", "/tmp/sportschau-mpv.sock")

RE_DATA_V = re.compile(r'data-v="([^"]*)"')
RE_SLOT = re.compile(r"/sportschau/radio/([a-z0-9]+)/", re.IGNORECASE)
RE_KICKOFF = re.compile(r"am (\d{2})\.(\d{2})\.(\d{4}) um (\d{1,2}):(\d{2}) Uhr")

# The CDN feed is up well before the broadcast (observed: >2 h before a
# 17:30 kickoff).  Slots are reused across matchdays, so only streams whose
# broadcast start is within this window are checked against the CDN at all.
# Broadcast starts 15 min before kickoff; 90 min match + break + overtime.
PRE_START = dt.timedelta(hours=3)
LIVE_AFTER_START = dt.timedelta(minutes=15 + 120)


def fetch(url: str, timeout: int = 15, max_bytes: int = 5 * 1024 * 1024) -> tuple[bytes, str]:
    """GET url, return (body, content_type). Reads at most max_bytes."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://www.sportschau.de/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        return resp.read(max_bytes), ctype


def derive_player_url(page_url: str) -> str | None:
    """https://.../<slug>,audiostream-x-104.html -> .../audiostream-x-104~player.html"""
    if "~player.html" in page_url:
        return page_url
    m = re.match(r"^(.*/)[^/]*?,([^,/]+)\.html(?:[?#].*)?$", page_url)
    if m:
        return f"{m.group(1)}{m.group(2)}~player.html"
    return None


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # "+0000" -> "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_kickoff(synopsis: str) -> dt.datetime | None:
    """'Anpfiff des Spiels ist am 13.09.2026 um 15:30 Uhr' -> aware datetime (Europe/Berlin)."""
    m = RE_KICKOFF.search(synopsis or "")
    if not m:
        return None
    d, mo, y, h, mi = (int(x) for x in m.groups())
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
    except Exception:  # no tzdata: assume CEST
        tz = dt.timezone(dt.timedelta(hours=2))
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz)


def extract_streams(raw_html: str, source_url: str = "") -> list[dict]:
    """All player configs embedded in a page -> list of stream records."""
    out = []
    for blob in RE_DATA_V.findall(raw_html):
        try:
            cfg = json.loads(htmllib.unescape(blob))
        except (json.JSONDecodeError, ValueError):
            continue
        mc = cfg.get("mc") if isinstance(cfg, dict) else None
        if not isinstance(mc, dict):
            continue
        meta = mc.get("meta") or {}
        urls = [m.get("url") for s in mc.get("streams") or []
                for m in s.get("media") or [] if m.get("url")]
        hls = next((u for u in urls if ".m3u8" in u), None)
        mp3 = next((u for u in urls if "icecast" in u or ".mp3" in u), None)
        if not (hls or mp3):
            continue
        slot_m = RE_SLOT.search(hls or mp3)
        live = mc.get("live") or {}
        page = ((mc.get("pluginData") or {}).get("sharing@web") or {}).get("link") or source_url
        title = (meta.get("title") or "").removeprefix("Live hören: ").strip()
        start = parse_iso(meta.get("broadcastedOnDateTime") or live.get("availFromDateTime"))
        out.append({
            "title": title,
            "slot": slot_m.group(1) if slot_m else None,
            "hls": hls,
            "mp3": mp3,
            "page": page,
            "start": start.isoformat() if start else None,
            "kickoff": (parse_kickoff(meta.get("synopsis") or "") or dt.datetime.min).isoformat()
                       if parse_kickoff(meta.get("synopsis") or "") else None,
            "dvr_seconds": live.get("dvrWindowSeconds"),
            "geo_flag": mc.get("isGeoBlocked"),
        })
    # de-duplicate by page URL, keep first
    seen, uniq = set(), []
    for s in out:
        if s["page"] in seen:
            continue
        seen.add(s["page"])
        uniq.append(s)
    return uniq


def in_live_window(stream: dict, now: dt.datetime | None = None) -> bool:
    """Schedule-based guess: from 3 h before broadcast start until ~2h15 after."""
    start = parse_iso(stream.get("start"))
    if not start:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    return start - PRE_START <= now <= start + LIVE_AFTER_START


def status_of(stream: dict, now: dt.datetime | None = None) -> str:
    """'live' (feed up, broadcast started), 'pre' (feed up, before broadcast), ''."""
    if not stream.get("live"):
        return ""
    start = parse_iso(stream.get("start"))
    now = now or dt.datetime.now(dt.timezone.utc)
    return "live" if (start is None or now >= start) else "pre"


def hls_is_live(hls_url: str) -> bool:
    """Cheap check: master playlist answers with #EXTM3U. Never touches the MP3."""
    try:
        body, _ = fetch(hls_url, timeout=8, max_bytes=64 * 1024)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
    return body.lstrip().startswith(b"#EXTM3U")


def find_streams(overview_urls=OVERVIEW_URLS, verify: bool = True, log=print) -> list[dict]:
    """Fetch the overview page(s), return all streams with a `live` field."""
    streams: list[dict] = []
    for u in overview_urls:
        log(f"[*] fetching {u}")
        try:
            body, _ = fetch(u)
        except Exception as e:
            log(f"[!]   failed: {e}")
            continue
        found = extract_streams(body.decode("utf-8", errors="replace"), u)
        log(f"[*]   {len(found)} stream(s)")
        streams.extend(found)
    now = dt.datetime.now(dt.timezone.utc)
    for s in streams:
        s["scheduled_live"] = in_live_window(s, now)
        # Only hit the CDN for streams that could plausibly be live.
        s["live"] = hls_is_live(s["hls"]) if (verify and s["hls"] and s["scheduled_live"]) else s["scheduled_live"]
    for s in streams:
        s["status"] = status_of(s, now)
    streams.sort(key=lambda s: (not s["live"], s["start"] or ""))
    return streams


def resolve_page(page_url: str, verify: bool = True, log=print) -> list[dict]:
    """One match page -> its stream record(s). Tries the ~player variant first."""
    targets = [t for t in (derive_player_url(page_url), page_url) if t]
    for t in targets:
        log(f"[*] fetching {t}")
        try:
            body, _ = fetch(t)
        except Exception as e:
            log(f"[!]   failed: {e}")
            continue
        found = extract_streams(body.decode("utf-8", errors="replace"), page_url)
        if found:
            for s in found:
                s["scheduled_live"] = in_live_window(s)
                s["live"] = hls_is_live(s["hls"]) if (verify and s["hls"]) else s["scheduled_live"]
                s["status"] = status_of(s)
            return found
        log("[!]   no player config found in page")
    return []


def fmt_local(iso: str | None) -> str:
    d = parse_iso(iso)
    return d.astimezone().strftime("%a %d.%m %H:%M") if d else "?"


def print_table(streams: list[dict]) -> None:
    for i, s in enumerate(streams, 1):
        mark = {"live": "LIVE", "pre": "pre "}.get(s["status"], "    ")
        print(f"{i:2d}. [{mark}] {fmt_local(s['kickoff'] or s['start'])}  "
              f"{s['slot'] or '?':<15} {s['title']}")


def mpv_command(stream: dict, prefer_mp3: bool = False) -> str:
    url = (stream["mp3"] if prefer_mp3 else stream["hls"]) or stream["hls"] or stream["mp3"]
    return f'mpv {MPV_FLAGS} --force-media-title="{stream["title"]}" "{url}"'


# ---------------------------------------------------------------- mpv control

class MpvError(RuntimeError):
    pass


class Mpv:
    """Minimal JSON-IPC client for a background mpv process.

    mpv is started once with --idle=yes and --input-ipc-server; every
    playback action is a command over the Unix socket.  If a socket from a
    previous run still answers, that mpv is reused instead of starting a
    second one.
    """

    def __init__(self, socket_path: str = MPV_SOCKET, extra_flags: str = MPV_FLAGS):
        self.path = socket_path
        self.flags = extra_flags
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.reader = None
        self._rid = 0
        self._edge: tuple[float, float] | None = None  # (newest buffered end, wall clock when seen)

    # -- lifecycle
    def ensure_running(self, timeout: float = 8.0) -> "Mpv":
        if self._connect():
            return self
        if os.path.exists(self.path):
            os.unlink(self.path)  # stale socket from a dead mpv
        cmd = ["mpv", "--idle=yes", "--terminal=no",
               f"--input-ipc-server={self.path}"] + shlex.split(self.flags)
        logfile = os.environ.get("SPORTSCHAU_MPV_LOG")  # debugging aid
        if logfile:
            cmd += [f"--log-file={logfile}", "--msg-level=all=v,ipc=debug"]
        else:
            cmd.append("--really-quiet")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connect():
                return self
            if self.proc.poll() is not None:
                raise MpvError(f"mpv exited immediately (code {self.proc.returncode}); is mpv installed?")
            time.sleep(0.1)
        raise MpvError(f"mpv did not open {self.path} within {timeout}s")

    def _connect(self) -> bool:
        if self.sock:
            return True
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(3.0)
            c.connect(self.path)
        except OSError:
            return False
        self.sock, self.reader = c, c.makefile("rb")
        try:
            self.send(["get_property", "mpv-version"])
        except MpvError:
            self.close()
            return False
        return True

    def close(self) -> None:
        for f in (self.reader, self.sock):
            try:
                f and f.close()
            except OSError:
                pass
        self.sock = self.reader = None

    def quit(self) -> None:
        """Stop mpv entirely and remove the socket."""
        try:
            self.send(["quit"])
        except MpvError:
            pass
        self.close()
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    # -- protocol
    def send(self, command: list) -> dict:
        if not self.sock:
            raise MpvError("not connected")
        self._rid += 1
        rid = self._rid
        try:
            self.sock.sendall((json.dumps({"command": command, "request_id": rid}) + "\n").encode())
            while True:
                line = self.reader.readline()
                if not line:
                    raise MpvError("mpv closed the connection")
                msg = json.loads(line)
                if msg.get("request_id") == rid:
                    if msg.get("error") not in (None, "success"):
                        raise MpvError(f"{command[0]}: {msg['error']}")
                    return msg
                # else: an event; ignore
        except (OSError, socket.timeout, ValueError) as e:
            self.close()
            raise MpvError(str(e)) from e

    def get(self, prop: str, default=None):
        try:
            return self.send(["get_property", prop]).get("data", default)
        except MpvError:
            return default

    # -- actions
    def play(self, url: str, title: str = "") -> None:
        if title:
            self.send(["set_property", "force-media-title", title])
        self.send(["loadfile", url, "replace"])
        self.send(["set_property", "pause", False])
        self._edge = None

    def toggle_pause(self) -> None:
        self.send(["cycle", "pause"])

    LIVE_MARGIN = 1.5  # seconds kept back from the buffered end to avoid EOF

    def seek(self, seconds: float) -> None:
        """Relative seek; forward seeks are clamped so we never run past the
        live edge (which makes mpv hit EOF and stop the stream)."""
        if seconds > 0:
            pos = self.get("time-pos")
            end = self.get("demuxer-cache-time")
            if pos is not None and end is not None:
                seconds = min(seconds, max(0.0, end - self.LIVE_MARGIN - pos))
                if seconds <= 0:
                    return
        self.send(["seek", seconds, "relative", "exact"])

    def seek_to(self, position: float) -> bool:
        """Absolute exact seek. Returns False if the position is no longer
        in mpv's buffer (then nothing is done)."""
        state = self.get("demuxer-cache-state") or {}
        ranges = state.get("seekable-ranges") or []
        if ranges and not any(r["start"] - 0.5 <= position <= r["end"] for r in ranges):
            return False
        self.send(["seek", position, "absolute", "exact"])
        return True

    def seek_live(self) -> None:
        """Jump to just before the newest buffered point (the live edge)."""
        end = self.get("demuxer-cache-time")
        if end is not None:
            self.send(["seek", max(0.0, end - self.LIVE_MARGIN), "absolute", "exact"])

    def volume(self, delta: float) -> None:
        self.send(["add", "volume", delta])

    def stop(self) -> None:
        self.send(["stop"])

    def status(self) -> dict:
        pos = self.get("time-pos")
        ranges = (self.get("demuxer-cache-state") or {}).get("seekable-ranges") or []
        start = min((r["start"] for r in ranges), default=None)
        end = max((r["end"] for r in ranges), default=None)
        # HLS delivers 4 s segments, so `end` jumps in steps.  The real live
        # edge moves continuously: extrapolate from the last step with wall
        # clock, and reset whenever the buffer moves backwards (new stream).
        now = time.monotonic()
        if end is None:
            self._edge = None
        elif self._edge is None or end < self._edge[0] - 1 or end > self._edge[0] + (now - self._edge[1]) + 0.5:
            self._edge = (end, now)
        live_edge = (self._edge[0] + (now - self._edge[1])) if self._edge else None
        return {
            "title": self.get("media-title"),
            "idle": self.get("idle-active", True),
            "paused": self.get("pause", False),
            "buffering": self.get("paused-for-cache", False),
            "pos": pos,
            "behind_live": max(0.0, live_edge - pos) if (pos is not None and live_edge is not None) else None,
            "rewind_room": (pos - start) if (pos is not None and start is not None) else None,
            "volume": self.get("volume"),
            "bitrate": self.get("audio-bitrate"),
        }


# ---------------------------------------------------------------- session

def _hms(secs) -> str:
    if secs is None:
        return "--:--"
    secs = int(secs)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 \
        else f"{secs // 60:02d}:{secs % 60:02d}"


class Session:
    """Player state shared by the terminal UI and the LAN web UI:
    the stream list, the current stream, the mark/sync state, one mpv.
    All public methods are serialised with a lock (mpv's socket is not
    thread-safe) and return a short human-readable message."""

    def __init__(self, verify: bool = True):
        import threading
        self.lock = threading.RLock()
        self.verify = verify
        self.streams: list[dict] = []
        self.current: int | None = None
        self.mark: tuple[float, float] | None = None   # (audio position, wall clock) of 1st press
        self.offset: float | None = None               # measured audio-ahead-of-picture delay
        self.mpv = Mpv()

    def start(self, log=lambda m: None) -> None:
        self.streams = find_streams(verify=self.verify, log=log)
        self.mpv.ensure_running()

    def _drop_mark(self, why: str) -> str:
        if self.mark is not None:
            self.mark = None
            return f"mark dropped ({why})"
        return ""

    def _guard(self):
        if not self.mpv._connect():
            self.mpv.ensure_running()

    # -- stream list
    def refresh(self) -> str:
        # Network I/O stays outside the lock so playback controls and /status
        # keep answering while the overview page and playlists are fetched.
        streams = find_streams(verify=self.verify, log=lambda m: None)
        with self.lock:
            cur_page = self.streams[self.current]["page"] if self.current is not None else None
            self.streams = streams
            self.current = next((i for i, s in enumerate(self.streams) if s["page"] == cur_page), None)
            return f"{len(self.streams)} streams, {sum(1 for s in self.streams if s['live'])} up"

    def play(self, index: int) -> str:
        with self.lock:
            if not 0 <= index < len(self.streams):
                return "no such stream"
            s = self.streams[index]
            self._drop_mark("stream changed")
            self.current = index
            self._guard()
            self.mpv.play(s["hls"] or s["mp3"], s["title"])
            if not s["live"]:
                return f"{s['title']}: feed not up yet (kickoff {fmt_local(s['kickoff'])}) — trying anyway"
            return f"playing {s['title']}"

    def next(self, step: int = 1) -> str:
        with self.lock:
            if self.current is None or not self.streams:
                return "nothing playing"
            return self.play((self.current + step) % len(self.streams))

    # -- transport
    def toggle_pause(self) -> str:
        with self.lock:
            if self.current is None:
                return ""
            msg = self._drop_mark("paused")
            self._guard()
            self.mpv.toggle_pause()
            return msg

    def seek(self, seconds: float) -> str:
        with self.lock:
            msg = self._drop_mark("position changed before 2nd press")
            self._guard()
            self.mpv.seek(seconds)
            return msg

    def seek_live(self) -> str:
        with self.lock:
            msg = self._drop_mark("position changed before 2nd press")
            self._guard()
            self.mpv.seek_live()
            return msg

    def volume(self, delta: float) -> str:
        with self.lock:
            self._guard()
            self.mpv.volume(delta)
            return ""

    def stop(self) -> str:
        with self.lock:
            self._drop_mark("stopped")
            self.current = None
            self._guard()
            self.mpv.stop()
            return "stopped"

    # -- mark / sync
    def mark_or_sync(self) -> str:
        with self.lock:
            if self.current is None:
                return "nothing playing"
            self._guard()
            pos = self.mpv.get("time-pos")
            if self.mark is None:
                if pos is None:
                    return "no position yet"
                self.mark = (pos, time.monotonic())
                return f"marked {_hms(pos)} — press again when the picture shows it"
            measured = time.monotonic() - self.mark[1]
            target = self.mark[0]
            self.mark = None
            if self.mpv.seek_to(target):
                self.offset = measured
                return f"synced: audio now {measured:.1f}s behind — fine-tune with ±1s / ±5s"
            return f"mark {_hms(target)} is no longer buffered; mark again"

    def clear_mark(self) -> str:
        with self.lock:
            self.mark, self.offset = None, None
            return "mark and sync cleared"

    # -- state
    def status(self) -> dict:
        with self.lock:
            try:
                st = self.mpv.status() if self.mpv.sock else {"idle": True}
            except MpvError:
                st = {"idle": True}
            st.update({
                "current": self.current,
                "mark_pos": self.mark[0] if self.mark else None,
                "mark_age": (time.monotonic() - self.mark[1]) if self.mark else None,
                "offset": self.offset,
            })
            return st

    def status_line(self, st: dict | None = None) -> str:
        st = st or self.status()
        if st.get("idle", True) or self.current is None:
            return "stopped"
        state = "BUFFER" if st.get("buffering") else ("PAUSED" if st.get("paused") else "playing")
        line = (f"{state}  {_hms(st.get('pos'))}  behind live {_hms(st.get('behind_live'))}  "
                f"rewind room {_hms(st.get('rewind_room'))}  vol {int(st.get('volume') or 0)}%")
        if st.get("mark_pos") is not None:
            line += f"  MARK {_hms(st['mark_pos'])} ({int(st['mark_age'])}s ago, press m when the picture catches up)"
        elif st.get("offset") is not None:
            line += f"  sync -{st['offset']:.1f}s"
        return line + f"  {st.get('title') or ''}"

    def shutdown(self, keep_mpv: bool = False) -> None:
        with self.lock:
            if keep_mpv:
                self.mpv.close()
            else:
                self.mpv.quit()


# ---------------------------------------------------------------- terminal UI

def _read_key(timeout: float) -> str | None:
    """One keypress (arrows mapped to names) or None on timeout. Raw mode assumed."""
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    ch = os.read(sys.stdin.fileno(), 1)
    if ch == b"\x1b":
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        seq = os.read(sys.stdin.fileno(), 2) if r else b""
        return {b"[A": "UP", b"[B": "DOWN", b"[C": "RIGHT", b"[D": "LEFT"}.get(seq, "ESC")
    if ch in (b"\r", b"\n"):
        return "ENTER"
    if ch == b"\x03":
        return "CTRL_C"
    return ch.decode("utf-8", errors="replace")


SEEK_KEYS = {",": -1, ".": 1, "<": -5, ">": 5, "LEFT": -10, "RIGHT": 10,
             "[": -60, "]": 60, "{": -300, "}": 300}

HELP = ("[1-9]+Enter play   space pause   seek: ,/. ±1s  </> ±5s  ←/→ ±10s  [/] ±1m  {/} ±5m   "
        "l live edge   m mark/sync   M clear   ↑/↓ volume   n/p next/prev   r refresh   s stop   d detach   q quit")


def run_ui(session: Session, banner: str = "") -> bool:
    """Interactive terminal front end. Returns True if mpv should keep running."""
    import termios
    import tty
    typed = ""
    flash = ""
    keep_mpv = False

    def draw():
        st = session.status()
        out = ["\x1b[H\x1b[J", "Sportschau Bundesliga audio" + (f"   {banner}" if banner else "") + "\n"]
        for i, s in enumerate(session.streams, 1):
            tag = {"live": "LIVE", "pre": "pre "}.get(s["status"], "    ")
            cur = ">" if i - 1 == session.current else " "
            out.append(f"{cur}{i:2d}. [{tag}] {fmt_local(s['kickoff'] or s['start'])}  {s['title']}")
        out.append("")
        out.append(session.status_line(st))
        out.append(flash or HELP)
        if typed:
            out.append(f"> {typed}")
        sys.stdout.write("\r\n".join(out) + "\r\n")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        live = [i for i, s in enumerate(session.streams) if s["status"] == "live"]
        if len(live) == 1 and session.current is None:
            flash = session.play(live[0])
        last_draw = 0.0
        while True:
            now = time.monotonic()
            if now - last_draw >= 0.5:
                draw()
                last_draw = now
            key = _read_key(0.2)
            if key is None:
                continue
            last_draw = 0.0  # redraw after every key
            try:
                if key in ("q", "CTRL_C"):
                    break
                elif key == "d":
                    keep_mpv = True
                    break
                elif key.isdigit():
                    typed += key
                    continue
                elif key == "ENTER" and typed:
                    n, typed = int(typed), ""
                    flash = session.play(n - 1)
                elif key == "ESC":
                    typed = ""
                elif key == " ":
                    flash = session.toggle_pause()
                elif key in SEEK_KEYS:
                    flash = session.seek(SEEK_KEYS[key])
                elif key == "l":
                    flash = session.seek_live()
                elif key == "m":
                    flash = session.mark_or_sync()
                elif key == "M":
                    flash = session.clear_mark()
                elif key == "UP":
                    flash = session.volume(5)
                elif key == "DOWN":
                    flash = session.volume(-5)
                elif key == "n":
                    flash = session.next(1)
                elif key == "p":
                    flash = session.next(-1)
                elif key == "s":
                    flash = session.stop()
                elif key == "r":
                    flash = "refreshing…"
                    draw()
                    flash = session.refresh()
            except MpvError as e:
                flash = f"mpv: {e}"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[H\x1b[J")
    return keep_mpv


# ---------------------------------------------------------------- LAN web UI

WEB_PAGE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Sportschau audio</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#111;color:#eee;font:16px/1.4 -apple-system,system-ui,sans-serif;padding:12px;max-width:640px;margin:auto}
 h1{font-size:18px;margin:4px 0 10px;color:#9cf}
 #status{background:#1c1c1c;border-radius:10px;padding:10px 12px;margin-bottom:10px;min-height:3.6em}
 #status .big{font-size:22px;font-weight:600}
 #status .dim{color:#999;font-size:14px}
 #flash{color:#fc6;font-size:14px;min-height:1.4em;margin:4px 0 8px}
 .row{display:flex;gap:6px;margin-bottom:8px}
 button{flex:1;background:#2a2a2a;color:#eee;border:0;border-radius:10px;padding:14px 4px;font-size:16px;touch-action:manipulation}
 button:active{background:#444}
 button.primary{background:#245;font-weight:600}
 button.mark{background:#552;font-size:18px;padding:18px}
 button.mark.armed{background:#c63;animation:pulse 1s infinite alternate}
 @keyframes pulse{from{opacity:1}to{opacity:.6}}
 button.danger{background:#522}
 #streams{list-style:none;padding:0;margin:12px 0 0}
 #streams li{display:flex;align-items:center;gap:8px;padding:10px 8px;border-bottom:1px solid #222;cursor:pointer}
 #streams li.current{background:#1a2a3a}
 #streams .tag{font-size:12px;width:34px;text-align:center;border-radius:6px;padding:2px 0;color:#000;background:#333}
 #streams .tag.live{background:#4c4}
 #streams .tag.pre{background:#cc4}
 #streams .when{color:#999;font-size:13px;width:88px}
 #streams .title{flex:1}
 small{color:#777}
</style></head><body>
<h1>Sportschau Bundesliga audio</h1>
<div id="status"><div class="big" id="state">…</div><div id="title"></div><div class="dim" id="detail"></div></div>
<div id="flash"></div>
<div class="row"><button class="primary" onclick="act('pause')">⏯ Pause</button><button onclick="act('live')">⏩ Live</button><button class="danger" onclick="act('stop')">⏹ Stop</button></div>
<div class="row"><button onclick="act('seek',{s:-300})">−5m</button><button onclick="act('seek',{s:-60})">−1m</button><button onclick="act('seek',{s:-10})">−10s</button><button onclick="act('seek',{s:-5})">−5s</button><button onclick="act('seek',{s:-1})">−1s</button></div>
<div class="row"><button onclick="act('seek',{s:300})">+5m</button><button onclick="act('seek',{s:60})">+1m</button><button onclick="act('seek',{s:10})">+10s</button><button onclick="act('seek',{s:5})">+5s</button><button onclick="act('seek',{s:1})">+1s</button></div>
<div class="row"><button class="mark" id="mark" onclick="act('mark')">● Mark</button><button onclick="act('clear')" style="flex:.4">✕</button></div>
<div class="row"><button onclick="act('volume',{d:-5})">🔉 −</button><button onclick="act('volume',{d:5})">🔊 +</button><button onclick="act('refresh')">↻ List</button></div>
<ul id="streams"></ul>
<small>Controls the mpv on the host machine. Home LAN only, no auth.</small>
<script>
const $=id=>document.getElementById(id);
const hms=s=>{if(s==null)return"--:--";s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+":"+String(m).padStart(2,"0"):String(m).padStart(2,"0"))+":"+String(x).padStart(2,"0")};
let flashTimer;
function flash(t){$("flash").textContent=t||"";clearTimeout(flashTimer);if(t)flashTimer=setTimeout(()=>$("flash").textContent="",6000)}
async function act(name,params){try{const r=await fetch("/"+name,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(params||{})});const j=await r.json();if(j.message)flash(j.message);render(j)}catch(e){flash("request failed: "+e)}}
function render(j){
 const st=j.status||{};
 if(st.idle||j.current==null){$("state").textContent="stopped";$("title").textContent="";$("detail").textContent=""}
 else{$("state").textContent=(st.buffering?"buffering":st.paused?"paused":"playing")+"  "+hms(st.pos);
  $("title").textContent=st.title||"";
  let d="behind live "+hms(st.behind_live)+" · rewind room "+hms(st.rewind_room)+" · vol "+Math.round(st.volume||0)+"%";
  if(st.offset!=null&&st.mark_pos==null)d+=" · sync −"+st.offset.toFixed(1)+"s";$("detail").textContent=d}
 const mk=$("mark");if(st.mark_pos!=null){mk.classList.add("armed");mk.textContent="● Marked "+hms(st.mark_pos)+" ("+Math.floor(st.mark_age)+"s) — tap when picture catches up"}else{mk.classList.remove("armed");mk.textContent="● Mark"}
 const ul=$("streams");ul.replaceChildren();
 (j.streams||[]).forEach((s,i)=>{const li=document.createElement("li");if(i===j.current)li.className="current";
  const span=(cls,text)=>{const e=document.createElement("span");e.className=cls;e.textContent=text;li.appendChild(e)};
  span("tag "+(s.status||""),s.status==="live"?"LIVE":s.status==="pre"?"pre":"");span("when",s.when);span("title",s.title);
  li.onclick=()=>act("play",{i});ul.appendChild(li)});
}
async function poll(){try{const r=await fetch("/status");render(await r.json())}catch(e){}}
poll();setInterval(poll,1000);
</script></body></html>
"""


def lan_ip() -> str:
    """Best-effort LAN address (no packets are sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def host_allowed(host_header: str | None) -> bool:
    """The page is reached by IP, localhost or a .local name.  Any other Host
    means a foreign web page whose hostname resolves to this machine (DNS
    rebinding), so refuse it."""
    host = urllib.parse.urlsplit("//" + (host_header or "")).hostname or ""
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """Browsers send Origin on every POST; it must be our own page.  No Origin
    (curl, scripts) is fine — the no-auth home-LAN model covers those."""
    if origin is None:
        return True
    return urllib.parse.urlsplit(origin).netloc == (host_header or "")


MAX_BODY = 64 * 1024


def make_server(session: Session, host: str, port: int):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def snapshot(message: str = "") -> dict:
        return {
            "message": message,
            "status": session.status(),
            "current": session.current,
            "streams": [{"title": s["title"], "status": s["status"], "live": s["live"],
                         "when": fmt_local(s["kickoff"] or s["start"]), "slot": s["slot"]}
                        for s in session.streams],
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not host_allowed(self.headers.get("Host")):
                return self._json({"error": "forbidden"}, 403)
            if self.path in ("/", "/index.html"):
                body = WEB_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/status":
                self._json(snapshot())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            host_header = self.headers.get("Host")
            if not (host_allowed(host_header) and origin_allowed(self.headers.get("Origin"), host_header)):
                return self._json({"error": "forbidden"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                return self._json({"error": "bad Content-Length"}, 400)
            try:
                params = json.loads(self.rfile.read(n)) if n else {}
            except ValueError:
                params = {}
            if not isinstance(params, dict):
                params = {}
            name = self.path.lstrip("/")
            try:
                if name == "play":
                    msg = session.play(int(params.get("i", -1)))
                elif name == "pause":
                    msg = session.toggle_pause()
                elif name == "seek":
                    msg = session.seek(float(params.get("s", 0)))
                elif name == "live":
                    msg = session.seek_live()
                elif name == "volume":
                    msg = session.volume(float(params.get("d", 0)))
                elif name == "stop":
                    msg = session.stop()
                elif name == "mark":
                    msg = session.mark_or_sync()
                elif name == "clear":
                    msg = session.clear_mark()
                elif name == "refresh":
                    msg = session.refresh()
                elif name == "next":
                    msg = session.next(int(params.get("step", 1)))
                else:
                    return self._json({"error": "unknown action"}, 404)
            except (MpvError, ValueError, TypeError) as e:
                msg = f"error: {e}"
            self._json(snapshot(msg))

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser(description="Find ARD Sportschau Bundesliga audio streams")
    ap.add_argument("url", nargs="?", help="a sportschau.de audiostream match page (optional)")
    ap.add_argument("--live", action="store_true",
                    help="only show streams whose feed is up now (live or pre-match)")
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    ap.add_argument("--mpv", action="store_true", help="print an mpv command for the first (live) stream")
    ap.add_argument("--mp3", action="store_true", help="with --mpv: use the Icecast MP3 instead of HLS")
    ap.add_argument("--no-verify", action="store_true", help="skip checking the CDN, use the schedule only")
    ap.add_argument("--ui", action="store_true", help="interactive terminal player (controls mpv over IPC)")
    ap.add_argument("--serve", nargs="?", const=8790, type=int, metavar="PORT",
                    help="LAN web UI on PORT (default 8790); combine with --ui to have both")
    ap.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)  # legacy alias for the list
    args = ap.parse_args()

    if args.ui or args.serve:
        return run_player(args)

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.url:
        streams = resolve_page(args.url, verify=not args.no_verify, log=log)
    else:
        streams = find_streams(verify=not args.no_verify, log=log)
    if args.live:
        streams = [s for s in streams if s["live"]]

    if not streams:
        log("[!] nothing found" + (" live" if args.live else "") +
            ". Check the overview page in a browser; if it changed, update OVERVIEW_URLS.")
        return 1

    if args.json:
        print(json.dumps(streams, indent=2, ensure_ascii=False))
    else:
        print_table(streams)
    if args.mpv:
        pick = next((s for s in streams if s["live"]), streams[0])
        print()
        print(mpv_command(pick, prefer_mp3=args.mp3))
    return 0


def run_player(args) -> int:
    import threading
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.ui and not sys.stdin.isatty():
        log("--ui needs an interactive terminal")
        return 2
    session = Session(verify=not args.no_verify)
    try:
        session.start(log=log)
    except MpvError as e:
        log(f"[!] {e}")
        return 1
    if not session.streams:
        log("[!] no streams found")
        session.shutdown()
        return 1

    server = None
    url = ""
    if args.serve:
        # macOS lets 0.0.0.0:PORT bind next to another process's 127.0.0.1:PORT,
        # so probe first instead of silently sharing the port.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        busy = probe.connect_ex(("127.0.0.1", args.serve)) == 0
        probe.close()
        if busy:
            log(f"[!] port {args.serve} is already in use on this machine; pick another with --serve PORT")
            session.shutdown()
            return 1
        try:
            server = make_server(session, "0.0.0.0", args.serve)
        except OSError as e:
            log(f"[!] cannot listen on port {args.serve}: {e}")
            session.shutdown()
            return 1
        url = f"http://{lan_ip()}:{args.serve}/"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log(f"[*] LAN control page: {url}")

    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    keep_mpv = False
    try:
        if args.ui:
            keep_mpv = run_ui(session, banner=url)
        else:
            log("[*] Ctrl-C to stop (mpv is stopped too)")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        session.shutdown(keep_mpv=keep_mpv)
        if keep_mpv:
            print(f"mpv keeps playing in the background (socket {session.mpv.path}); "
                  f"run --ui again to reattach.")
        else:
            print("mpv stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
