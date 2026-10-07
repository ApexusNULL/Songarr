"""Adding whole albums from search (soundtracks, film scores…): Deezer album search and track lists,
matching against the library, and queueing the missing songs in album order."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr import discover
from songarr.appapi import make_app_server
from songarr.service import REQUEST_PRIORITY, Service

from tests.helpers import FakeYouTube, spotify_track
from tests.test_platform import free_port

SCORE = 7001  # an expanded film score: 150 tracks over two discs, so the track list comes in pages
OTHER = 7002  # a different film by the same composer, which also has a "Main Title"


def score_track(n: int) -> dict:
    title = "Main Title" if n == 1 else f"Cue {n}"
    return {"id": 90000 + n, "title": title, "isrc": f"USSC{n:08d}", "track_position": (n - 1) % 75 + 1,
            "disk_number": 1 if n <= 75 else 2, "duration": 120 + n, "explicit_lyrics": False, "artist": {"name": "Ada Composer"}}


FAKE = {
    "/search/album?q=space%20score&limit=20": {"data": [
        {"id": 7003, "title": "Space Score (Single Edit)", "record_type": "single", "nb_tracks": 1, "artist": {"name": "Ada Composer"}},
        {"id": SCORE, "title": "Space Score (Original Motion Picture Soundtrack)", "record_type": "album", "nb_tracks": 150,
         "artist": {"name": "Ada Composer"}, "cover_xl": "https://img/score-xl.jpg", "cover_medium": "https://img/score-m.jpg"},
    ]},
    f"/album/{SCORE}": {"id": SCORE, "title": "Space Score (Original Motion Picture Soundtrack)", "record_type": "album",
                        "release_date": "2014-11-18", "label": "Film Music Records", "artist": {"name": "Ada Composer"},
                        "contributors": [{"name": "Ada Composer"}], "genres": {"data": [{"name": "Films/Games"}]},
                        "cover_xl": "https://img/score-xl.jpg", "cover_medium": "https://img/score-m.jpg"},
    f"/album/{SCORE}/tracks?index=0&limit=100": {"total": 150, "data": [score_track(n) for n in range(1, 101)]},
    f"/album/{SCORE}/tracks?index=100&limit=100": {"total": 150, "data": [score_track(n) for n in range(101, 151)]},
}


def fake_get(path: str, ttl: float) -> dict:
    if path in FAKE:
        return FAKE[path]
    if path.startswith("/album/404"):
        raise RuntimeError("Deezer: no data")
    raise OSError(f"offline: {path}")


class AlbumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_get = discover._get
        discover._get = fake_get
        cls.dir = Path(tempfile.mkdtemp())
        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        db = cls.svc.db
        # Already on the server: cue 2 of the score (same recording, from someone's Spotify, so a
        # different id and album name), and the *other* film's "Main Title" by the same composer.
        db.upsert_tracks([
            spotify_track(1, title="Cue 2", artists=["Ada Composer"], album="Space Score", album_artists=["Ada Composer"],
                          isrc="USSC00000002"),
            spotify_track(2, title="Main Title", artists=["Ada Composer"], album="Other Film", album_artists=["Ada Composer"],
                          isrc="USOT00000001"),
        ])
        for n in (1, 2):
            db.set_status(f"{n:022d}", "downloaded", file_path=str(cls.dir / f"{n}.m4a"), file_size=1)
        cls.port = free_port()
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        code, _ = cls.svc.users.new_pairing_code(1)
        cls.token = cls.call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        discover._get = cls.orig_get
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def call(cls, token, method, path, body=None):
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

    def test_1_search_finds_albums_full_albums_first(self):
        st, res = self.call(self.token, "GET", "/api/v1/catalog/albums?q=space%20score")
        self.assertEqual(st, 200)
        self.assertEqual([(a["id"], a["type"]) for a in res["albums"]], [(SCORE, "album"), (7003, "single")])
        self.assertEqual(res["albums"][0]["count"], 150)

    def test_2_album_shows_every_track_and_what_is_already_here(self):
        st, a = self.call(self.token, "GET", f"/api/v1/catalog/albums/deezer/{SCORE}")
        self.assertEqual(st, 200)
        self.assertEqual((a["count"], a["year"], a["label"], a["added"]), (150, "2014", "Film Music Records", False))
        self.assertEqual([(t["disc_number"], t["track_number"]) for t in a["tracks"][74:76]], [(1, 75), (2, 1)])
        by_title = {t["title"]: t for t in a["tracks"]}
        self.assertEqual(by_title["Cue 2"]["in_library"], f"{1:022d}")  # same recording (ISRC)
        self.assertTrue(by_title["Cue 2"]["track"]["playable"])
        self.assertIsNone(by_title["Main Title"]["in_library"])  # another film's "Main Title" isn't this one
        self.assertEqual(a["on_server"], 1)

    def test_3_adding_the_album_queues_the_rest_in_order(self):
        st, a = self.call(self.token, "POST", "/api/v1/requests/album", {"source": "deezer", "id": SCORE})
        self.assertEqual((st, a["added"], a["on_server"]), (200, True, 1))
        db = self.svc.db
        rows = db.q("SELECT * FROM tracks WHERE album_id = ? ORDER BY requested_at DESC", (f"dz{SCORE}",))
        self.assertEqual(len(rows), 149)  # cue 2 wasn't downloaded again
        self.assertEqual([(r["disc_number"], r["track_number"]) for r in rows[:3]], [(1, 1), (1, 3), (1, 4)])  # album order
        self.assertTrue(all(r["status"] == "wanted" and r["priority"] == REQUEST_PRIORITY and r["monitored"] for r in rows))
        self.assertEqual(rows[0]["album_artists"], json.dumps(["Ada Composer"]))
        # the copy that was already here is now in this person's library too, with its own details kept
        mine = {r[0] for r in db.q("SELECT track_id FROM track_sources WHERE source = 'req:1'")}
        self.assertIn(f"{1:022d}", mine)
        self.assertEqual(db.one("SELECT album FROM tracks WHERE id = ?", (f"{1:022d}",))[0], "Space Score")
        self.assertEqual(db.q("SELECT COUNT(*) FROM history WHERE event = 'requested'")[0][0], 1)  # one line, not 150
        # adding it again changes nothing
        self.call(self.token, "POST", "/api/v1/requests/album", {"source": "deezer", "id": SCORE})
        self.assertEqual(db.one("SELECT COUNT(*) FROM tracks WHERE album_id = ?", (f"dz{SCORE}",))[0], 149)

    def test_4_newest_request_first_then_the_album_then_the_backlog(self):
        db = self.svc.db
        db.upsert_tracks([spotify_track(50, title="Backlog Song", added_at="2030-01-01T00:00:00Z")])  # newest like
        db.run("UPDATE tracks SET monitored = 1 WHERE id = ?", (f"{50:022d}",))
        late = spotify_track(60, title="Asked For Later")
        self.svc.request_track(1, late)  # after the album
        order = []
        while (row := db.claim_next()) is not None:
            order.append(row["title"])
        self.assertEqual(order[0], "Asked For Later")  # the newest request goes first
        self.assertEqual(order[1:4], ["Main Title", "Cue 3", "Cue 4"])  # then the album, in order
        self.assertEqual(order[-1], "Backlog Song")  # the backlog waits for every request
        self.assertEqual(len(order), 1 + 149 + 1)

    def test_5_errors(self):
        self.assertEqual(self.call(self.token, "POST", "/api/v1/requests/album", {"source": "spotify", "id": "x"})[0], 400)
        self.assertEqual(self.call(self.token, "GET", "/api/v1/catalog/albums/deezer/404")[0], 404)
        self.assertEqual(self.call(self.token, "GET", "/api/v1/catalog/albums/deezer/555")[0], 502)  # Deezer unreachable
        self.assertEqual(self.call(self.token, "GET", "/api/v1/catalog/albums?q=offline")[0], 502)
        self.assertEqual(self.call(None, "GET", f"/api/v1/catalog/albums/deezer/{SCORE}")[0], 401)


if __name__ == "__main__":
    unittest.main()
