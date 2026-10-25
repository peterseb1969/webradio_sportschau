"""Offline tests: finder against the saved 2026-09-13 pages, plus the web
server's request guards.  Run with `python3 -m unittest` from the repo root."""

import datetime as dt
import http.client
import json
import os
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sportschau_stream as ss  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")


def fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


UTC = dt.timezone.utc


class ExtractOverview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.streams = ss.extract_streams(fixture("overview-2026-09-13.html"), "src")

    def test_count(self):
        # 11 matches + Konferenz, de-duplicated from 15 data-v blobs
        self.assertEqual(len(self.streams), 12)
        self.assertEqual(len({s["page"] for s in self.streams}), 12)

    def test_first_record(self):
        s = self.streams[0]
        self.assertEqual(s["title"], "RB Leipzig gegen Hamburger SV - Bundesliga")
        self.assertEqual(s["slot"], "liga1spiel8")
        self.assertEqual(s["hls"],
                         "https://sportschau-radio.ard-mcdn.de/sportschau/radio/liga1spiel8/hls/master.m3u8")
        self.assertIn("icecastssl.wdr.de", s["mp3"])
        self.assertEqual(s["start"], "2026-09-13T13:15:00+00:00")
        self.assertEqual(s["kickoff"], "2026-09-13T15:30:00+02:00")
        self.assertEqual(s["dvr_seconds"], 7200)
        self.assertIs(s["geo_flag"], True)
        self.assertTrue(s["page"].startswith("https://www.sportschau.de/"))

    def test_every_record_complete(self):
        for s in self.streams:
            with self.subTest(s["title"]):
                self.assertFalse(s["title"].startswith("Live hören"))
                self.assertTrue(s["hls"].endswith("/hls/master.m3u8"))
                self.assertIsNotNone(s["mp3"])
                self.assertRegex(s["slot"], r"^liga1(spiel\d|konferenz)$")
                self.assertIsNotNone(s["start"])
                self.assertIsNotNone(s["kickoff"])

    def test_broadcast_starts_before_kickoff(self):
        # single matches: 15 min before kickoff; the Konferenz: 20 min
        for s in self.streams:
            with self.subTest(s["title"]):
                gap = ss.parse_iso(s["kickoff"]) - ss.parse_iso(s["start"])
                want = 20 if s["slot"] == "liga1konferenz" else 15
                self.assertEqual(gap, dt.timedelta(minutes=want))

    def test_konferenz_present(self):
        self.assertIn("liga1konferenz", [s["slot"] for s in self.streams])


class ExtractPlayerPage(unittest.TestCase):
    def test_single_stream(self):
        streams = ss.extract_streams(fixture("player-elversberg-bayern-2026-09-13.html"), "src")
        self.assertEqual(len(streams), 1)
        s = streams[0]
        self.assertEqual(s["slot"], "liga1spiel9")
        self.assertEqual(s["title"], "SV 07 Elversberg gegen Bayern München - Bundesliga")
        self.assertEqual(s["kickoff"], "2026-09-13T17:30:00+02:00")
        self.assertIn("audiostream-sv-07-elversberg-gegen-bayern-muenchen", s["page"])

    def test_garbage(self):
        self.assertEqual(ss.extract_streams('<div data-v="not json">'), [])
        self.assertEqual(ss.extract_streams('<div data-v="{&quot;mc&quot;:{}}">'), [])

    def test_malformed_blob_skipped(self):
        # a blob with an unexpected shape is dropped, the good ones survive
        good = fixture("player-elversberg-bayern-2026-09-13.html")
        bad = [{"streams": ["x"]}, {"streams": [{"media": [{"url": 5}]}]},
               {"meta": "x", "streams": [{"media": [{"url": "https://h/a.m3u8"}]}]},
               {"streams": [{"media": [{"url": "https://h/a.m3u8"}]}],
                "pluginData": {"sharing@web": {"link": ["not", "a", "string"]}}}]
        blobs = "".join('<div data-v="%s">' % json.dumps({"mc": mc}).replace('"', "&quot;")
                        for mc in bad)
        streams = ss.extract_streams(blobs + good, "src")
        self.assertIn("liga1spiel9", [s["slot"] for s in streams])
        self.assertIn("src", [s["page"] for s in streams])  # non-string link -> source URL


class Parsing(unittest.TestCase):
    def test_parse_iso(self):
        want = dt.datetime(2026, 9, 13, 13, 15, tzinfo=UTC)
        self.assertEqual(ss.parse_iso("2026-09-13T13:15:00Z"), want)
        self.assertEqual(ss.parse_iso("2026-09-13T13:15:00+0000"), want)
        self.assertEqual(ss.parse_iso("2026-09-13T15:15:00+02:00"), want)
        self.assertEqual(ss.parse_iso("2026-09-13T13:15:00"), want)  # no offset -> UTC
        self.assertIsNone(ss.parse_iso(None))
        self.assertIsNone(ss.parse_iso("nonsense"))

    def test_parse_kickoff(self):
        k = ss.parse_kickoff("3. Spieltag: Anpfiff des Spiels ist am 13.09.2026 um 15:30 Uhr. Mehr")
        self.assertEqual(k.astimezone(UTC), dt.datetime(2026, 9, 13, 13, 30, tzinfo=UTC))
        self.assertIsNone(ss.parse_kickoff("kein Datum"))
        self.assertIsNone(ss.parse_kickoff(None))
        self.assertIsNone(ss.parse_kickoff("am 31.02.2026 um 15:30 Uhr"))
        self.assertIsNone(ss.parse_kickoff("am 13.09.2026 um 24:00 Uhr"))

    def test_derive_player_url(self):
        base = "https://www.sportschau.de/fussball/bundesliga/"
        self.assertEqual(ss.derive_player_url(base + "a-gegen-b,audiostream-a-gegen-b-104.html"),
                         base + "audiostream-a-gegen-b-104~player.html")
        self.assertEqual(ss.derive_player_url(base + "x,audiostream-x-100.html?foo=1#bar"),
                         base + "audiostream-x-100~player.html")
        self.assertEqual(ss.derive_player_url(base + "audiostream-x-100~player.html"),
                         base + "audiostream-x-100~player.html")
        self.assertIsNone(ss.derive_player_url("https://example.com/no-comma"))


class Timing(unittest.TestCase):
    stream = {"start": "2026-09-13T13:15:00+00:00"}  # Leipzig–HSV broadcast start

    def at(self, h, m):
        return dt.datetime(2026, 9, 13, h, m, tzinfo=UTC)

    def test_live_window(self):
        self.assertFalse(ss.in_live_window(self.stream, self.at(10, 14)))
        self.assertTrue(ss.in_live_window(self.stream, self.at(10, 15)))   # start - 3 h
        self.assertTrue(ss.in_live_window(self.stream, self.at(15, 30)))   # start + 2 h 15
        self.assertFalse(ss.in_live_window(self.stream, self.at(15, 31)))
        self.assertFalse(ss.in_live_window({"start": None}, self.at(13, 0)))
        self.assertTrue(ss.in_live_window({"start": "2026-09-13T13:15:00"}, self.at(13, 0)))

    def test_status(self):
        up = dict(self.stream, live=True)
        self.assertEqual(ss.status_of(up, self.at(12, 0)), "pre")
        self.assertEqual(ss.status_of(up, self.at(13, 15)), "live")
        self.assertEqual(ss.status_of(dict(self.stream, live=False), self.at(13, 30)), "")


class PlaylistCheck(unittest.TestCase):
    def test_any_fetch_error_means_not_live(self):
        errors = (http.client.IncompleteRead(b"#EXT"), http.client.BadStatusLine("junk"),
                  OSError("down"), ValueError("unknown url type"))
        for err in errors:
            with self.subTest(type(err).__name__), mock.patch.object(ss, "fetch", side_effect=err):
                self.assertFalse(ss.hls_is_live("https://h/master.m3u8"))

    def test_playlist_answer(self):
        with mock.patch.object(ss, "fetch", return_value=(b"\n#EXTM3U\n", "")):
            self.assertTrue(ss.hls_is_live("https://h/master.m3u8"))
        with mock.patch.object(ss, "fetch", return_value=(b"<html>", "")):
            self.assertFalse(ss.hls_is_live("https://h/master.m3u8"))


class EarlyExitKeepsReusedMpv(unittest.TestCase):
    """`--ui` after a detach reuses the running mpv; failing to start must not quit it."""

    def run_player(self, started_mpv: bool):
        def start(session, log):  # overview fetch came back empty
            session.streams = []
            session.mpv.proc = object() if started_mpv else None

        args = mock.Mock(ui=False, serve=8790, no_verify=True)
        with mock.patch.object(ss.Session, "start", start), \
                mock.patch.object(ss.Session, "shutdown") as shutdown, \
                mock.patch("sys.stderr"):
            self.assertEqual(ss.run_player(args), 1)
        return shutdown

    def test_reused_mpv_kept(self):
        self.run_player(started_mpv=False).assert_called_once_with(keep_mpv=True)

    def test_own_mpv_quit(self):
        self.run_player(started_mpv=True).assert_called_once_with(keep_mpv=False)


class MpvMissing(unittest.TestCase):
    def test_friendly_error(self):
        mpv = ss.Mpv(socket_path="/nonexistent-dir/mpv.sock")
        with mock.patch.object(ss.subprocess, "Popen", side_effect=FileNotFoundError("mpv")):
            with self.assertRaisesRegex(ss.MpvError, "brew install mpv"):
                mpv.ensure_running()


class FakeMpv:
    """Just enough of Mpv for the session logic: a position on a buffered timeline."""

    def __init__(self, pos=1000.0, path=None, title=None):
        self.pos = pos
        self.props = {"time-pos": pos, "path": path, "media-title": title, "idle-active": path is None}
        self.played = []

    def _connect(self):
        return True

    def get(self, prop, default=None):
        if prop == "time-pos":
            return self.pos
        value = self.props.get(prop)
        return default if value is None else value

    def play(self, url, title=""):
        self.played.append(url)

    def seek_to(self, position):
        self.pos = position
        return True

    def seek(self, seconds):
        self.pos += seconds
        return seconds

    def seek_live(self):
        pass


class MarkSync(unittest.TestCase):
    def setUp(self):
        self.session = ss.Session(verify=False)
        self.session.mpv = self.mpv = FakeMpv()
        self.session.current = 0

    def mark_and_sync(self, gap):
        """m, audio plays on for `gap` seconds, m."""
        self.session.mark_or_sync()
        self.mpv.pos += gap
        return self.session.mark_or_sync()

    def test_repeated_sync_adds_up(self):
        start = self.mpv.pos
        self.assertIn("now 30.0s behind", self.mark_and_sync(30))
        self.mpv.pos += 60  # a minute later the user notices audio is still ahead
        second_mark = self.mpv.pos
        self.assertIn("+4.0s, audio now 34.0s behind", self.mark_and_sync(4))
        self.assertEqual(self.session.offset, 34.0)
        # back at the 2nd mark, not the 1st, and 34 s behind where the audio
        # would be without syncing (30 + 60 + 4 s played)
        self.assertEqual(self.mpv.pos, second_mark)
        self.assertNotEqual(self.mpv.pos, start)
        self.assertEqual(start + 30 + 60 + 4 - self.mpv.pos, 34.0)

    def test_fine_tune_adjusts_offset(self):
        self.mark_and_sync(30)
        self.session.seek(-1)
        self.session.seek(5)
        self.assertEqual(self.session.offset, 26.0)

    def test_live_and_stop_clear_sync(self):
        self.mark_and_sync(30)
        self.assertIn("sync cleared", self.session.seek_live())
        self.assertIsNone(self.session.offset)
        self.mark_and_sync(30)
        with mock.patch.object(FakeMpv, "stop", create=True):
            self.session.stop()
        self.assertIsNone(self.session.offset)


def stream(page, start, live=False):
    return {"page": page, "start": start, "live": live, "hls": f"https://h/{page}/master.m3u8",
            "mp3": None, "title": page, "status": "live" if live else "", "kickoff": None}


class Views(unittest.TestCase):
    now = dt.datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    streams = [stream("live", "2026-09-13T11:15:00+00:00", live=True),
               stream("later", "2026-09-13T16:15:00+00:00"),
               stream("over", "2026-09-13T07:00:00+00:00"),       # ended 09:15
               stream("next-week", "2026-09-20T13:15:00+00:00")]

    def test_views(self):
        vis = lambda view, cur=None: ss.visible_indices(self.streams, view, cur, self.now)  # noqa: E731
        self.assertEqual(vis("live"), [0])
        self.assertEqual(vis("today"), [0, 1])
        self.assertEqual(vis("all"), [0, 1, 2, 3])
        self.assertEqual(vis("live", cur=3), [0, 3])  # the playing stream always shows

    def test_play_row_uses_on_screen_numbers(self):
        session = ss.Session(verify=False, view="all")
        session.mpv = mpv = FakeMpv()
        session.streams = [dict(s) for s in self.streams]
        with mock.patch.object(ss, "visible_indices", lambda st, v, c: [1, 3]):
            self.assertTrue(session.play_row(2).startswith("next-week: feed not up yet"))
            self.assertEqual(session.play_row(3), "no such stream")
        self.assertEqual(session.current, 3)
        self.assertEqual(mpv.played, [self.streams[3]["hls"]])


class NextPrev(unittest.TestCase):
    def test_cycles_live_streams_only(self):
        session = ss.Session(verify=False)
        session.mpv = FakeMpv()
        session.streams = [stream("a", None, live=True), stream("b", None), stream("c", None, live=True)]
        session.current = 0
        session.next(1)
        self.assertEqual(session.current, 2)
        session.next(1)
        self.assertEqual(session.current, 0)
        session.next(-1)
        self.assertEqual(session.current, 2)
        session.streams[0]["live"] = False
        self.assertEqual(session.next(1), "no other live stream")


class Reattach(unittest.TestCase):
    def setUp(self):
        self.session = ss.Session(verify=False)
        self.session.streams = [stream("a", None, live=True), stream("b", None, live=True)]

    def test_finds_playing_stream(self):
        self.session.mpv = FakeMpv(path=self.session.streams[1]["hls"], title="B")
        self.session._adopt_playing()
        self.assertEqual(self.session.current, 1)

    def test_unknown_stream_adopted(self):
        self.session.mpv = FakeMpv(path="https://h/old/master.m3u8", title="Last week")
        self.session._adopt_playing()
        self.assertEqual(self.session.streams[self.session.current]["title"], "Last week")
        self.assertEqual(len(self.session.streams), 3)

    def test_idle_mpv_left_alone(self):
        self.session.mpv = FakeMpv()
        self.session._adopt_playing()
        self.assertIsNone(self.session.current)

    def test_refresh_keeps_stream_that_left_the_overview(self):
        self.session.current = 1
        with mock.patch.object(ss, "find_streams", lambda **kw: [stream("c", None, live=True)]):
            self.session.refresh()
        self.assertEqual([s["page"] for s in self.session.streams], ["c", "b"])
        self.assertEqual(self.session.current, 1)


class RefreshDoesNotHoldLock(unittest.TestCase):
    def test_lock_free_during_fetch(self):
        session = ss.Session(verify=False)
        session.streams = [{"page": "a"}, {"page": "b"}]
        session.current = 1
        seen = {}

        def fake_find(**kw):
            # try the lock from another thread, as the web UI would
            t = threading.Thread(target=lambda: seen.update(
                free=session.lock.acquire(timeout=1) and (session.lock.release() or True)))
            t.start()
            t.join()
            return [{"page": "b", "live": True}, {"page": "c", "live": False}]

        with mock.patch.object(ss, "find_streams", fake_find):
            session.refresh()
        self.assertTrue(seen["free"])
        self.assertEqual(session.current, 0)  # current stream followed to its new index


class StubSession:
    current = None
    streams = []

    def view(self, view=None):
        return self.streams, [], self.current

    def status(self):
        return {"idle": True}

    def play(self, index):
        return f"play {index}"


class WebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ss.make_server(StubSession(), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type") == "application/json" else data)

    def test_page_and_status(self):
        code, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertNotIn(b"innerHTML", body)
        self.assertEqual(self.request("GET", "/status")[0], 200)

    def test_same_origin_post(self):
        host = f"127.0.0.1:{self.port}"
        code, body = self.request("POST", "/play", json.dumps({"i": 2}),
                                  {"Origin": f"http://{host}", "Content-Type": "application/json"})
        self.assertEqual((code, body["message"]), (200, "play 2"))

    def test_post_without_origin(self):
        code, body = self.request("POST", "/play", json.dumps({"i": 0}))
        self.assertEqual((code, body["message"]), (200, "play 0"))

    def test_cross_origin_post_refused(self):
        for origin in ("http://evil.example", "null"):
            with self.subTest(origin):
                code, _ = self.request("POST", "/play", '{"i": 0}',
                                       {"Origin": origin, "Content-Type": "text/plain"})
                self.assertEqual(code, 403)

    def test_dns_rebinding_refused(self):
        headers = {"Host": f"evil.example:{self.port}", "Origin": f"http://evil.example:{self.port}"}
        self.assertEqual(self.request("POST", "/play", "{}", headers)[0], 403)
        self.assertEqual(self.request("GET", "/status", headers={"Host": "evil.example"})[0], 403)

    def test_bad_content_length(self):
        for value in ("abc", "-5", str(ss.MAX_BODY + 1)):
            with self.subTest(value):
                code, _ = self.request("POST", "/play", headers={"Content-Length": value})
                self.assertEqual(code, 400)

    def test_bad_params(self):
        for body in ('{"i": [1]}', '{"i": "x"}', "[1, 2]", "not json"):
            with self.subTest(body):
                code, resp = self.request("POST", "/play", body)
                self.assertEqual(code, 200)
                self.assertTrue(resp["message"].startswith("error:") or resp["message"] == "play -1")


class HostCheck(unittest.TestCase):
    def test_host_allowed(self):
        for h in ("192.168.1.20:8790", "127.0.0.1", "localhost:8790", "[::1]:8790", "peters-mac.local:8790"):
            self.assertTrue(ss.host_allowed(h), h)
        for h in ("evil.example:8790", "", None, "192.168.1.20.nip.io"):
            self.assertFalse(ss.host_allowed(h), h)


if __name__ == "__main__":
    unittest.main()
