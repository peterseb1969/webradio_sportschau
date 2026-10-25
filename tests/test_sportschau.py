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


class Parsing(unittest.TestCase):
    def test_parse_iso(self):
        want = dt.datetime(2026, 9, 13, 13, 15, tzinfo=UTC)
        self.assertEqual(ss.parse_iso("2026-09-13T13:15:00Z"), want)
        self.assertEqual(ss.parse_iso("2026-09-13T13:15:00+0000"), want)
        self.assertEqual(ss.parse_iso("2026-09-13T15:15:00+02:00"), want)
        self.assertIsNone(ss.parse_iso(None))
        self.assertIsNone(ss.parse_iso("nonsense"))

    def test_parse_kickoff(self):
        k = ss.parse_kickoff("3. Spieltag: Anpfiff des Spiels ist am 13.09.2026 um 15:30 Uhr. Mehr")
        self.assertEqual(k.astimezone(UTC), dt.datetime(2026, 9, 13, 13, 30, tzinfo=UTC))
        self.assertIsNone(ss.parse_kickoff("kein Datum"))
        self.assertIsNone(ss.parse_kickoff(None))

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

    def test_status(self):
        up = dict(self.stream, live=True)
        self.assertEqual(ss.status_of(up, self.at(12, 0)), "pre")
        self.assertEqual(ss.status_of(up, self.at(13, 15)), "live")
        self.assertEqual(ss.status_of(dict(self.stream, live=False), self.at(13, 30)), "")


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
