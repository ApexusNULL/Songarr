"""Adding an artist's whole discography from their page: the releases (one of each edition), and each
song queued once, however many releases it's on and whether or not the server has it already."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr import discover
from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube, spotify_track
from tests.test_platform import free_port

BAND = 8100
FIRST, FIRST_CLEAN, SECOND, LIVE, HITS, SINGLE, LOST = 8201, 8202, 8203, 8206, 8204, 8205, 8207


def release(rid: int, title: str, date: str, kind: str = "album", explicit: bool = False) -> dict:
    return {"id": rid, "title": title, "release_date": date, "record_type": kind, "explicit_lyrics": explicit,
            "cover_xl": f"https://img/{rid}.jpg", "cover_medium": f"https://img/{rid}-m.jpg"}


def song(sid: int, title: str, isrc: str, seconds: int) -> dict:
    return {"id": sid, "title": title, "isrc": isrc, "duration": seconds, "explicit_lyrics": False, "artist": {"name": "Nova Band"}}


ALBUMS = {
    FIRST: (release(FIRST, "First Light", "2001-03-01", explicit=True),
            [song(1, "Hit One", "NB0000000001", 200), song(2, "Deep Cut", "NB0000000002", 240), song(3, "Hit Three", "NB0000000003", 210)]),
    SECOND: (release(SECOND, "Second Wind", "2005-06-01"),
             [song(4, "Hit Two", "NB0000000004", 190), song(5, "Slow One", "NB0000000005", 300)]),
    LIVE: (release(LIVE, "Live at Home", "2008-01-01"),  # the same title, played live: much longer, so another song
           [song(6, "Hit One", "NB0000000006", 260)]),
    HITS: (release(HITS, "Greatest Hits", "2010-01-01"),  # Deezer calls hits albums "album" too
           [song(7, "Hit One", "NB0000000001", 200), song(8, "Hit Two", "NB0000000004", 190),
            song(9, "Hit Three - 2010 Remaster", "NB1000000009", 211), song(10, "Bonus Hit", "NB0000000010", 180)]),
    SINGLE: (release(SINGLE, "New Song", "2012-05-05", "single"), [song(11, "New Song", "NB0000000011", 170)]),
}
LISTING = [release(SINGLE, "New Song", "2012-05-05", "single"), release(LOST, "Lost Tapes", "2015-01-01"),
           release(HITS, "Greatest Hits", "2010-01-01"), release(FIRST_CLEAN, "First Light", "2001-04-01"),
           release(LIVE, "Live at Home", "2008-01-01"), release(FIRST, "First Light", "2001-03-01", explicit=True),
           release(SECOND, "Second Wind", "2005-06-01")]
gate = threading.Event()  # held closed to keep an add running
gate.set()


def fake_get(path: str, ttl: float) -> dict:
    if path == f"/artist/{BAND}/albums?limit=100&index=0":
        return {"data": LISTING, "total": len(LISTING)}
    if path.startswith("/artist/404/"):
        raise RuntimeError("Deezer: no data")
    if path.startswith("/search/artist?q=Nova%20Band"):
        return {"data": [{"id": BAND, "name": "Nova Band", "picture_medium": "https://img/band-m.jpg"}]}
    for rid, (meta, tracks) in ALBUMS.items():
        if path == f"/album/{rid}":
            gate.wait(10)
            return meta | {"artist": {"name": "Nova Band"}, "contributors": [{"name": "Nova Band"}]}
        if path == f"/album/{rid}/tracks?index=0&limit=100":
            return {"total": len(tracks), "data": [t | {"track_position": n} for n, t in enumerate(tracks, 1)]}
    raise OSError(f"offline: {path}")


class DiscographyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_get = discover._get
        discover._get = fake_get
        cls.dir = Path(tempfile.mkdtemp())
        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        # On the server already, from someone's Spotify: Hit Two, from a remastered edition (another
        # recording code and album name, a second longer).
        cls.svc.db.upsert_tracks([spotify_track(1, title="Hit Two", artists=["Nova Band"], album="Second Wind (Remastered)",
                                                album_artists=["Nova Band"], isrc="NBRM00000004", duration_ms=191_000)])
        cls.svc.db.set_status(f"{1:022d}", "downloaded", file_path=str(cls.dir / "1.m4a"), file_size=1)
        cls.port = free_port()
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        code, _ = cls.svc.users.new_pairing_code(1)
        cls.token = cls.call("POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"}, token=None)[1]["token"]

    @classmethod
    def tearDownClass(cls):
        discover._get = cls.orig_get
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def call(cls, method, path, body=None, token=""):
        token = cls.token if token == "" else token
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def wait_until_added(self):
        for _ in range(200):
            st, d = self.call("GET", f"/api/v1/artist/discography?deezer_id={BAND}&name=Nova%20Band")
            if d["adding"] is None:
                return d
            time.sleep(0.05)
        self.fail("the discography never finished adding")

    def test_1_one_of_each_release_newest_first(self):
        st, d = self.call("GET", f"/api/v1/artist/discography?deezer_id={BAND}&name=Nova%20Band")
        self.assertEqual(st, 200)
        self.assertEqual([r["id"] for r in d["releases"]], [LOST, SINGLE, HITS, LIVE, SECOND, FIRST])  # the clean twin is gone
        first = d["releases"][-1]
        self.assertEqual((first["name"], first["year"], first["type"], first["artists"]), ("First Light", "2001", "album", ["Nova Band"]))
        self.assertIsNone(d["adding"])

    def test_2_each_song_once_in_the_order_it_came_out(self):
        st, started = self.call("POST", "/api/v1/requests/artist",
                                {"deezer_id": BAND, "name": "Nova Band", "ids": [FIRST, SECOND, LIVE, HITS, SINGLE, LOST, 999]})
        self.assertEqual((st, started["adding"]["of"]), (200, 6))  # 999 isn't theirs
        self.wait_until_added()
        db = self.svc.db
        order = []
        while (row := db.claim_next()) is not None:
            order.append((row["title"], row["album"]))
        self.assertEqual(order, [
            ("Hit One", "First Light"), ("Deep Cut", "First Light"), ("Hit Three", "First Light"),
            ("Slow One", "Second Wind"),  # Hit Two is on the server already
            ("Hit One", "Live at Home"),  # same title, but the live one
            ("Bonus Hit", "Greatest Hits"),  # the only song that isn't on an album
            ("New Song", "New Song"),
        ])
        mine = {r[0] for r in db.q("SELECT track_id FROM track_sources WHERE source = 'req:1'")}
        self.assertIn(f"{1:022d}", mine)  # the Hit Two that was here joins this library, details kept
        self.assertEqual(db.one("SELECT album FROM tracks WHERE id = ?", (f"{1:022d}",))[0], "Second Wind (Remastered)")
        self.assertEqual(len(mine), 8)
        self.assertEqual(db.one("SELECT COUNT(*) FROM tracks WHERE title LIKE 'Hit Three%'")[0], 1)  # not the remaster again
        self.assertEqual([r[0] for r in db.q("SELECT message FROM history WHERE event = 'requested'")],
                         [f"{self.who()} added Nova Band's discography (6 releases, 8 songs, 7 new)"])  # one line, not 8
        st, n = self.call("GET", "/api/v1/notifications")
        note = n["notifications"][0]
        self.assertEqual((note["kind"], note["title"], note["artist"], note["image_url"]),
                         ("discography", "Nova Band: discography added", "Nova Band", "https://img/band-m.jpg"))
        self.assertEqual(note["body"], "7 new songs downloading, 1 already on your server, 1 release couldn't be read (try again later)")

    def test_3_again_adds_nothing_new(self):
        before = self.svc.db.one("SELECT COUNT(*) FROM tracks")[0]
        self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "name": "Nova Band", "ids": [FIRST, HITS]})
        self.wait_until_added()
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM tracks")[0], before)
        st, n = self.call("GET", "/api/v1/notifications")
        self.assertEqual(n["notifications"][0]["body"], "All 5 songs were already on your server")  # 3 + Hit Two + Bonus Hit

    def test_4_one_add_at_a_time(self):
        gate.clear()
        try:
            st, d = self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "name": "Nova Band", "ids": [FIRST]})
            self.assertEqual(st, 200)
            st, d = self.call("GET", f"/api/v1/artist/discography?deezer_id={BAND}&name=Nova%20Band")
            self.assertEqual(d["adding"], {"done": 0, "of": 1, "songs": 0})
            self.assertEqual(self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "name": "Nova Band", "ids": [SECOND]})[0], 409)
        finally:
            gate.set()
        self.wait_until_added()

    def test_5_errors(self):
        self.assertEqual(self.call("GET", "/api/v1/artist/discography")[0], 400)
        self.assertEqual(self.call("GET", "/api/v1/artist/discography?deezer_id=404&name=x")[0], 404)
        self.assertEqual(self.call("GET", "/api/v1/artist/discography?deezer_id=555&name=x")[0], 502)  # Deezer unreachable
        self.assertEqual(self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "name": "Nova Band", "ids": []})[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "name": "Nova Band", "ids": [999]})[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/requests/artist", {"deezer_id": BAND, "ids": [FIRST]})[0], 400)
        self.assertEqual(self.call("GET", f"/api/v1/artist/discography?deezer_id={BAND}", token=None)[0], 401)

    def who(self) -> str:
        return self.svc.db.one("SELECT name FROM users WHERE id = 1")[0]


if __name__ == "__main__":
    unittest.main()
