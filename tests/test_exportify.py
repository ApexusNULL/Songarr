"""Bringing someone's Spotify library over from an Exportify export (no Spotify requests at all)."""

from __future__ import annotations

import base64
import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from songarr import exportify
from songarr.appapi import AppAPI
from songarr.service import Service
from songarr.web import make_server

from tests.helpers import FakeYouTube, spotify_track

HEADER = ["Track URI", "Track Name", "Artist URI(s)", "Artist Name(s)", "Album URI", "Album Name", "Album Artist URI(s)",
          "Album Artist Name(s)", "Album Release Date", "Album Image URL", "Disc Number", "Track Number",
          "Track Duration (ms)", "Track Preview URL", "Explicit", "Popularity", "ISRC", "Added By", "Added At"]


def row(n: int, added: str = "2026-01-01T00:00:00Z", artists: str | None = None, added_by: str = "") -> dict:
    return {"Track URI": f"spotify:track:{n:022d}", "Track Name": f"Song {n}", "Artist Name(s)": artists or f"Artist {n}",
            "Album URI": f"spotify:album:{n:022d}", "Album Name": f"Album {n}", "Album Artist Name(s)": artists or f"Artist {n}",
            "Album Release Date": "2020-05-01", "Album Image URL": "https://i.scdn.co/image/ab67616d0000b273cafe",
            "Disc Number": "1", "Track Number": str(n), "Track Duration (ms)": "201000", "Explicit": "false",
            "ISRC": f"US{n:010d}", "Added By": added_by, "Added At": added}


def exportify_csv(rows: list[dict]) -> bytes:
    """As Exportify writes it: every field in double quotes, "" for a quote, \\n between rows."""
    q = lambda v: '"' + str(v).replace('"', '""') + '"'  # noqa: E731
    lines = [",".join(q(h) for h in HEADER)] + [",".join(q(r.get(h, "")) for h in HEADER) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def tid(n: int) -> str:
    return f"{n:022d}"


class ReadTests(unittest.TestCase):
    def test_a_row_becomes_a_track(self):
        rows = [row(1, artists="Tyler\\, The Creator, Kali Uchis"),
                {"Track URI": "spotify:local:Someone:Something:Song:200", "Track Name": "Song", "Artist Name(s)": "Someone"},
                row(2) | {"Track Name": 'He said "hi"'},
                row(4) | {"Track Name": ""}]  # Spotify keeps pulled songs in libraries with no title
        tracks = exportify.read_csv("﻿" + exportify_csv(rows).decode())
        self.assertEqual([t["id"] for t in tracks], [tid(1), tid(2)])  # the local file is left out
        t = tracks[0]
        self.assertEqual(t["artists"], ["Tyler, The Creator", "Kali Uchis"])
        self.assertEqual((t["album"], t["album_id"], t["release_date"], t["track_number"], t["duration_ms"], t["isrc"]),
                         ("Album 1", tid(1), "2020-05-01", 1, 201000, f"US{1:010d}"))
        self.assertEqual(t["thumb_url"], "https://i.scdn.co/image/ab67616d00004851cafe")  # Spotify's small cover
        self.assertEqual(tracks[1]["title"], 'He said "hi"')

    def test_not_an_exportify_file(self):
        with self.assertRaises(exportify.ExportifyError) as e:
            exportify.read_csv("Title,Artist\nSong,Someone\n")
        self.assertIn("Exportify", str(e.exception))

    def test_which_file_is_liked_songs(self):
        self.assertEqual([exportify.is_liked(s) for s in ("liked", "liked (1)", "Liked", "liked_rock", "road_trip")],
                         [True, True, True, False, False])
        self.assertEqual(exportify.playlist_name("road_trip"), "Road trip")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir, 8484, youtube_factory=lambda s: FakeYouTube())
        self.jamie = self.svc.users.create("Jamie")
        self.api = AppAPI(self.svc)

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_liked_songs_become_their_likes_in_spotify_order(self):
        self.svc.db.upsert_tracks([spotify_track(2)])  # already in the library (someone else's like)
        self.svc.db.set_status(tid(2), "downloaded", file_path="x.m4a")
        data = exportify_csv([row(3, "2026-03-01T10:00:00Z"), row(2, "2026-02-01T10:00:00Z"), row(1, "2025-12-24T08:00:00Z")])
        r = exportify.import_upload(self.svc, self.jamie, "liked.csv", data)
        self.assertEqual((r["liked"], r["liked_new"], r["new_songs"], r["playlists"]), (3, 3, 2, []))
        self.assertEqual(self.svc.db.liked_list(self.jamie), [tid(3), tid(2), tid(1)])  # newest like first
        status = dict(self.svc.db.q("SELECT id, status FROM tracks WHERE monitored = 1"))
        self.assertEqual(status, {tid(1): "wanted", tid(2): "downloaded", tid(3): "wanted"})  # new ones download
        again = exportify.import_upload(self.svc, self.jamie, "liked.csv", data)
        self.assertEqual((again["liked_new"], again["new_songs"]), (0, 0))  # importing again doubles nothing
        self.assertEqual(len(self.svc.db.liked_list(self.jamie)), 3)

    def test_export_all_brings_likes_and_playlists(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("liked.csv", exportify_csv([row(1), row(2)]))
            z.writestr("road_trip.csv", exportify_csv([row(5, added_by="spotify:user:g"), row(1, added_by="spotify:user:g"),
                                                       row(4, added_by="spotify:user:g")]))
        r = exportify.import_upload(self.svc, self.jamie, "spotify_playlists.zip", buf.getvalue())
        self.assertEqual((r["liked"], r["playlists"]), (2, [{"name": "Road trip", "songs": 3}]))
        me = {"id": self.jamie, "name": "Jamie"}
        pl = self.api.library(me, {}, {})["playlists"]
        mine = [p for p in pl if p["name"] == "Road trip"]
        self.assertEqual(len(mine), 1)
        songs = self.api.playlist(me, {}, {}, mine[0]["id"])["tracks"]
        self.assertEqual([t["id"] for t in songs], [tid(5), tid(1), tid(4)])  # Spotify's order
        again = exportify.import_upload(self.svc, self.jamie, "spotify_playlists.zip", buf.getvalue())
        self.assertEqual((again["playlists"], again["skipped"]), ([], ["Road trip"]))  # not twice

    def test_nothing_usable(self):
        with self.assertRaises(exportify.ExportifyError):
            exportify.import_upload(self.svc, self.jamie, "liked.csv", exportify_csv([]))


class AdminPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.svc = Service(self.dir, self.port, youtube_factory=lambda s: FakeYouTube())
        self.http = make_server(self.svc, "127.0.0.1", self.port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.jamie = self.svc.users.create("Jamie")

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-Songarr": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_import_from_the_people_page(self):
        data = base64.b64encode(exportify_csv([row(1), row(2)])).decode()
        status, r = self.post(f"/api/users/{self.jamie}/import", {"filename": "liked.csv", "data": data})
        self.assertEqual((status, r["liked"], r["new_songs"]), (200, 2, 2))
        status, r = self.post(f"/api/users/{self.jamie}/import", {"filename": "notes.csv",
                                                                    "data": base64.b64encode(b"a,b\n1,2\n").decode()})
        self.assertEqual(status, 400)
        self.assertIn("Exportify", r["error"])
        status, _ = self.post("/api/users/999/import", {"filename": "liked.csv", "data": data})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
