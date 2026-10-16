"""Lyrics from LRCLIB: finding the song when it's written a little differently there, and telling a
busy LRCLIB apart from "no lyrics". All offline (a little web server stands in for LRCLIB)."""

from __future__ import annotations

import http.server
import json
import threading
import unittest

from songarr import lyrics

ONE_SYNCED = "[00:01.00] la la\n[00:03.00] la"


class PickTests(unittest.TestCase):
    def test_the_same_song_written_otherwise(self):
        results = [
            {"trackName": "Under Pressure", "artistName": "Queen & David Bowie", "duration": 248, "syncedLyrics": ONE_SYNCED},
        ]
        hit = lyrics._pick(results, ["Queen", "David Bowie"], "Under Pressure (feat. David Bowie) - Remastered 2011", 247)
        self.assertEqual(hit["syncedLyrics"], ONE_SYNCED)
        self.assertIsNone(lyrics._pick(results, ["Someone Else"], "Under Pressure", 247))  # another artist's song

    def test_timed_words_only_for_the_same_length(self):
        results = [{"trackName": "Song", "artistName": "Band", "duration": 200, "syncedLyrics": ONE_SYNCED}]
        self.assertEqual(lyrics._pick(results, ["Band"], "Song", 203)["syncedLyrics"], ONE_SYNCED)
        hit = lyrics._pick(results, ["Band"], "Song", 215)  # 15 s off: another cut of it, so the words only
        self.assertEqual((hit["syncedLyrics"], hit["plainLyrics"]), (None, "la la\nla"))
        self.assertIsNone(lyrics._pick(results, ["Band"], "Song", 260))  # nothing like it

    def test_the_best_one(self):
        results = [
            {"trackName": "Song", "artistName": "Band", "duration": 210, "plainLyrics": "words"},
            {"trackName": "Song", "artistName": "Band", "duration": 201, "syncedLyrics": ONE_SYNCED},
            {"trackName": "Song", "artistName": "Band", "duration": 200, "instrumental": True},
        ]
        self.assertEqual(lyrics._pick(results, ["Band"], "Song", 200)["syncedLyrics"], ONE_SYNCED)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.orig = lyrics._get

    def tearDown(self):
        lyrics._get = self.orig

    def test_a_plain_search_when_the_artist_is_spelt_otherwise(self):
        def fake(path, **q):
            self.asked.append((path, q))
            if "q" in q:
                return [{"trackName": "Copper Skies", "artistName": "N0RTHBOUND & Friend", "duration": 174, "plainLyrics": "words"}]
            return None if path == "get" else []
        lyrics._get = fake
        found = lyrics.fetch("N0rthbound", "Copper Skies", "Copper Skies", 174)
        self.assertEqual(found["plain"], "words")
        self.assertEqual([p for p, _ in self.asked], ["get", "search", "search"])
        self.assertEqual(self.asked[-1][1], {"q": "N0rthbound Copper Skies"})


class _LRCLIB(http.server.BaseHTTPRequestHandler):
    answers: list = []  # (status, headers, body), one per request
    seen: list = []

    def do_GET(self):
        _LRCLIB.seen.append(self.path)
        status, headers, body = _LRCLIB.answers.pop(0) if _LRCLIB.answers else (404, {}, b"")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class BusyTests(unittest.TestCase):
    def setUp(self):
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LRCLIB)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.api, lyrics.API = lyrics.API, f"http://127.0.0.1:{self.http.server_port}"
        self.spacing, lyrics.SPACING = lyrics.SPACING, 0.0
        lyrics._quiet_until = 0.0
        _LRCLIB.answers, _LRCLIB.seen = [], []

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        lyrics.API, lyrics.SPACING, lyrics._quiet_until = self.api, self.spacing, 0.0

    def test_a_stumble_is_tried_again(self):
        _LRCLIB.answers = [(503, {}, b""), (200, {}, json.dumps([{"ok": 1}]).encode())]
        self.assertEqual(lyrics._get("search", q="x"), [{"ok": 1}])
        self.assertEqual(len(_LRCLIB.seen), 2)

    def test_slow_down_is_respected(self):
        _LRCLIB.answers = [(429, {"Retry-After": "30"}, b"")]
        with self.assertRaises(lyrics.Busy):
            lyrics._get("search", q="x")
        with self.assertRaises(lyrics.Busy):  # and it isn't asked again meanwhile
            lyrics._get("search", q="y")
        self.assertEqual(len(_LRCLIB.seen), 1)

    def test_nothing_there(self):
        _LRCLIB.answers = [(404, {}, b""), (400, {}, b"")]
        self.assertIsNone(lyrics._get("get", track_name="x"))
        self.assertIsNone(lyrics._get("get", track_name="y"))


if __name__ == "__main__":
    unittest.main()
