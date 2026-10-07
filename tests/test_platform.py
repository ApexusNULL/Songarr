"""Family profiles, pairing, the app API and YouTube sign-in (all offline)."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr import discover, playlists
from songarr.appapi import make_app_server
from songarr.db import DB
from songarr.service import REQUEST_PRIORITY, Service
from songarr.users import SignInError

from tests.helpers import FFMPEG, FakeYouTube, audio_file, spotify_track


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fresh_install_gets_a_main_profile(self):
        db = DB(self.dir / "a.db")
        self.assertEqual([tuple(r) for r in db.q("SELECT name, is_admin FROM users")], [("Me", 1)])
        db.close()

    def test_single_spotify_connection_becomes_the_first_profiles_likes(self):
        db = DB(self.dir / "b.db")
        db.run("DELETE FROM users")  # pretend this database predates profiles
        for k, v in {"spotify_refresh_token": "ref", "spotify_access_token": "acc", "spotify_expires_at": 1.0,
                     "spotify_user": "Alex", "spotify_user_id": "alex1", "spotify_scopes": "user-library-read"}.items():
            db.set_setting(k, v)
        db.upsert_tracks([spotify_track(1)])
        db.add_source(f"{1:022d}", "liked")
        db.close()

        db = DB(self.dir / "b.db")
        self.assertEqual([tuple(r) for r in db.q("SELECT id, name, is_admin FROM users")], [(1, "Alex", 1)])
        self.assertEqual(db.liked_list(1), [f"{1:022d}"])
        self.assertEqual(db.one("SELECT source FROM track_sources")[0], "like:1")
        self.assertIsNone(db.one("SELECT 1 FROM settings WHERE key GLOB 'spotify_*'"))
        self.assertEqual(db.recompute_monitored(), (0, 0))  # nothing lost its source
        db.close()


# The Spotify tables of versions that read Spotify (before Exportify).
OLD_SPOTIFY_TABLES = """
CREATE TABLE spotify_accounts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, spotify_id TEXT UNIQUE, display_name TEXT,
    access_token TEXT, refresh_token TEXT, expires_at REAL, scopes TEXT, connected REAL, last_sync REAL, sync_error TEXT);
CREATE TABLE account_playlists(account_id INTEGER NOT NULL, playlist_id TEXT NOT NULL, PRIMARY KEY(account_id, playlist_id));
CREATE TABLE playlists(id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT, owner_id TEXT, collaborative INTEGER NOT NULL DEFAULT 0,
    snapshot_id TEXT, image_url TEXT, total INTEGER, enabled INTEGER NOT NULL DEFAULT 1, readable INTEGER,
    present INTEGER NOT NULL DEFAULT 1, error TEXT, synced REAL, m3u_path TEXT);
CREATE TABLE playlist_tracks(playlist_id TEXT NOT NULL, position INTEGER NOT NULL, track_id TEXT NOT NULL, added_at TEXT,
    PRIMARY KEY(playlist_id, position));
CREATE TABLE playlist_dismissed(user_id INTEGER NOT NULL, source_id TEXT NOT NULL, PRIMARY KEY(user_id, source_id));
CREATE TABLE unlikes(user_id INTEGER NOT NULL, track_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user_id, track_id));
CREATE TABLE account_shows(account_id INTEGER NOT NULL, podcast_id TEXT NOT NULL, PRIMARY KEY(account_id, podcast_id));
CREATE TABLE spotify_shows(spotify_id TEXT PRIMARY KEY, podcast_id TEXT, checked REAL NOT NULL);
"""


class RetireSpotifyTests(unittest.TestCase):
    """Songarr no longer reads Spotify: what linked Spotify accounts brought in becomes people's own."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_what_spotify_brought_in_becomes_peoples_own(self):
        tid = lambda n: f"{n:022d}"  # noqa: E731
        path = self.dir / "songarr.db"
        db = DB(path)
        sam = db.run_insert("INSERT INTO users(name, is_admin, created) VALUES ('Sam', 0, 0)")
        db.upsert_tracks([spotify_track(n) for n in range(1, 6)])
        db.conn.executescript(OLD_SPOTIFY_TABLES)
        with db.tx() as c:
            c.execute("INSERT INTO spotify_accounts(id, user_id, refresh_token) VALUES (7, 1, 'secret')")
            c.execute("INSERT INTO spotify_accounts(id, user_id, refresh_token) VALUES (8, ?, 'secret too')", (sam,))
            # Me's Liked Songs on Spotify: 2 (newest), 1, and 3, which Me unliked in Songarr; 4 was liked in the app
            for pos, (n, when) in enumerate([(2, "2026-03-01T00:00:00Z"), (1, "2026-02-01T00:00:00Z"), (3, "2026-01-01T00:00:00Z")]):
                c.execute("INSERT INTO playlist_tracks VALUES ('liked:7', ?, ?, ?)", (pos, tid(n), when))
                c.execute("INSERT INTO track_sources VALUES (?, 'liked:7')", (tid(n),))
            c.execute("INSERT INTO unlikes VALUES (1, ?, 0)", (tid(3),))
            c.execute("INSERT INTO likes VALUES (1, ?, ?)", (tid(4), time.time()))
            c.execute("INSERT INTO track_sources VALUES (?, 'like:1')", (tid(4),))
            # a Spotify playlist of Sam's, which was already Sam's own playlist too
            c.execute("INSERT INTO playlists(id, name, m3u_path) VALUES ('plroad', 'Road Trip', ?)",
                      (str(self.dir / "Playlists" / "Road Trip.m3u8"),))
            c.execute("INSERT INTO playlist_tracks VALUES ('plroad', 0, ?, NULL)", (tid(5),))
            c.execute("INSERT INTO track_sources VALUES (?, 'pl:plroad')", (tid(5),))
            c.execute("""INSERT INTO user_playlists(id, user_id, name, created, updated, source_id)
                         VALUES ('upcopy', ?, 'Road Trip', 0, 0, 'plroad')""", (sam,))
            c.execute("INSERT INTO user_playlist_tracks VALUES ('upcopy', 0, ?, 0)", (tid(5),))
            c.execute("INSERT INTO track_sources VALUES (?, 'up:upcopy')", (tid(5),))
            c.execute("INSERT INTO account_shows VALUES (8, 'pod1')")  # a podcast Sam saved on Spotify
            c.execute("""INSERT INTO settings VALUES ('spotify_client_id', '"0123"'), ('sync_new_playlists', 'true'),
                                                     ('spotify_rate_limited_until', '0')""")
        db.recompute_monitored()
        db.close()

        db = DB(path)  # the first start after the update
        self.assertEqual(db.liked_list(1), [tid(4), tid(2), tid(1)])  # in the same order; the unliked one stays unliked
        self.assertEqual(db.liked_list(sam), [])
        self.assertEqual([r[0] for r in db.q("SELECT podcast_id FROM podcast_follows WHERE user_id = ?", (sam,))], ["pod1"])
        self.assertEqual([r[0] for r in db.q("SELECT name FROM user_playlists WHERE user_id = ?", (sam,))], ["Road Trip"])
        self.assertEqual(sorted(r[0] for r in db.q("SELECT DISTINCT source FROM track_sources")), ["like:1", "up:upcopy"])
        self.assertEqual({r[0] for r in db.q("SELECT id FROM tracks WHERE monitored = 1")}, {tid(1), tid(2), tid(4), tid(5)})
        tables = {r[0] for r in db.q("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse(tables & {"spotify_accounts", "playlists", "playlist_tracks", "account_playlists", "account_shows",
                                   "spotify_shows", "unlikes", "playlist_dismissed"})
        self.assertIsNone(db.one("SELECT 1 FROM settings WHERE key GLOB 'spotify_*' OR key = 'sync_new_playlists'"))
        self.assertEqual(db.setting("m3u_files"), ["Road Trip.m3u8"])  # so the next playlist write tidies it up
        self.assertIn("2 liked songs and 1 saved podcast ", db.one("SELECT message FROM history WHERE event = 'system'")[0])
        old = sqlite3.connect(self.dir / "songarr-before-spotify-removal.db")  # the database from before is kept
        self.assertEqual(old.execute("SELECT COUNT(*) FROM spotify_accounts").fetchone()[0], 2)
        old.close()
        db.close()
        db = DB(path)  # only once
        self.assertEqual(db.liked_list(1), [tid(4), tid(2), tid(1)])
        self.assertEqual(db.one("SELECT COUNT(*) FROM history WHERE event = 'system'")[0], 1)
        db.close()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir, 8484, youtube_factory=lambda s: FakeYouTube())

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_removing_a_profile_cleans_up(self):
        db, tid = self.svc.db, lambda n: f"{n:022d}"  # noqa: E731
        sam = self.svc.users.create("Sam")
        db.upsert_tracks([spotify_track(1), spotify_track(2)])
        with db.tx() as c:
            c.executemany("INSERT INTO likes VALUES (?, ?, 0)", [(sam, tid(1)), (sam, tid(2)), (1, tid(2))])
            c.executemany("INSERT INTO track_sources VALUES (?, ?)", [(tid(1), f"like:{sam}"), (tid(2), f"like:{sam}"), (tid(2), "like:1")])
            c.execute("INSERT INTO user_playlists(id, user_id, name, created, updated) VALUES ('upsam', ?, 'Mix', 0, 0)", (sam,))
            c.execute("INSERT INTO user_playlist_tracks VALUES ('upsam', 0, ?, 0)", (tid(1),))
            c.execute("INSERT INTO track_sources VALUES (?, 'up:upsam')", (tid(1),))
        db.recompute_monitored()
        self.svc.users.delete(sam)
        self.assertEqual({r[0] for r in db.q("SELECT id FROM tracks WHERE monitored = 0")}, {tid(1)})  # song 2 is still Me's
        self.assertEqual(db.one("SELECT COUNT(*) FROM likes WHERE user_id = ?", (sam,))[0], 0)
        self.assertIsNone(db.one("SELECT 1 FROM user_playlists WHERE user_id = ?", (sam,)))
        with self.assertRaises(ValueError):
            self.svc.users.delete(1)  # the main profile stays


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir, 8484, youtube_factory=lambda s: FakeYouTube())
        self.users = self.svc.users

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_codes_work_once_and_tokens_authenticate(self):
        code, _ = self.users.new_pairing_code(1)
        token, uid = self.users.pair(f"{code[:5]}-{code[5:].lower()}", "Pixel 9", "1.2.3.4")  # dash and case don't matter
        self.assertEqual(uid, 1)
        self.assertIsNone(self.users.pair(code, "again", "1.2.3.4"))
        self.assertEqual(self.users.authenticate(token, "1.2.3.4")["name"], "Me")
        self.assertIsNone(self.users.authenticate(token + "x", "1.2.3.4"))
        self.assertNotIn(token, json.dumps([dict(r) for r in self.svc.db.q("SELECT * FROM devices")]))  # stored hashed
        self.users.revoke_token(token)
        self.assertIsNone(self.users.authenticate(token, "1.2.3.4"))

    def test_expired_codes_and_guessing_are_refused(self):
        code, _ = self.users.new_pairing_code(1)
        self.svc.db.run("UPDATE pairing_codes SET expires = 0")
        self.assertIsNone(self.users.pair(code, "x", "5.5.5.5"))
        for _ in range(10):
            self.users.pair("WRONGCODE2", "x", "6.6.6.6")
        good, _ = self.users.new_pairing_code(1)
        self.assertIsNone(self.users.pair(good, "x", "6.6.6.6"))  # this address is locked out for 10 minutes
        self.assertIsNotNone(self.users.pair(good, "x", "7.7.7.7"))

    def test_passwords(self):
        sam = self.users.create("Sam")
        with self.assertRaises(ValueError):
            self.users.set_password(sam, "sam", "short")
        self.users.set_password(sam, " Sam ", "correct horse battery")
        with self.assertRaises(ValueError):
            self.users.set_password(1, "SAM", "another long one")  # names are unique, ignoring case
        stored = self.svc.db.one("SELECT password_hash FROM users WHERE id = ?", (sam,))[0]
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("horse", stored)
        listed = next(u for u in self.users.list() if u["id"] == sam)
        self.assertEqual((listed["login"], listed["has_password"]), ("Sam", True))
        self.assertNotIn("password_hash", listed)

        token, uid = self.users.sign_in("sam", "correct horse battery", "Pixel", "1.1.1.1")
        self.assertEqual((uid, self.users.authenticate(token, "1.1.1.1")["name"]), (sam, "Sam"))
        with self.assertRaises(SignInError) as e:
            self.users.sign_in("Sam", "wrong password", "Pixel", "1.1.1.1")
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(SignInError):
            self.users.sign_in("nobody", "correct horse battery", "Pixel", "1.1.1.1")
        for i in range(4):  # five wrong tries in all (from several addresses): the name waits 10 minutes
            with self.assertRaises(SignInError):
                self.users.sign_in("sam", f"guess {i}", "x", f"9.9.9.{i}")
        with self.assertRaises(SignInError) as e:
            self.users.sign_in("sam", "correct horse battery", "x", "8.8.8.8")
        self.assertEqual(e.exception.status, 429)

        self.users.set_password(1, "alex", "my own password")
        self.assertEqual(self.users.sign_in("Alex", "my own password", "PC", "8.8.8.8")[1], 1)  # others unaffected
        with self.assertRaises(SignInError) as e:
            self.users.change_password(1, "alex", "not it", "a new password", "8.8.8.8")
        self.assertEqual(e.exception.status, 403)
        self.users.change_password(1, "alex", "my own password", "a new password", "8.8.8.8")
        self.assertEqual(self.users.sign_in("alex", "a new password", "PC", "8.8.8.8")[1], 1)
        self.users.clear_password(1)
        with self.assertRaises(SignInError):
            self.users.sign_in("alex", "a new password", "PC", "8.8.8.8")


@unittest.skipUnless(FFMPEG, "FFmpeg not installed")
class AppAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        db, tid = cls.svc.db, lambda n: f"{n:022d}"  # noqa: E731
        db.upsert_tracks([spotify_track(1), spotify_track(2, title="Second Song", isrc="USX000000002"), spotify_track(3),
                          spotify_track(77, title="Brand New Hit", isrc="USNEW0000077")])
        # Me likes songs 1 and 2 (2 most recently) and has Road Trip, brought over from Spotify (songs 1 and 3)
        with db.tx() as c:
            c.executemany("INSERT INTO likes(user_id, track_id, created) VALUES (1, ?, ?)", [(tid(1), 1000.0), (tid(2), 2000.0)])
            c.executemany("INSERT INTO track_sources(track_id, source) VALUES (?, 'like:1')", [(tid(1),), (tid(2),)])
            c.execute("""INSERT INTO user_playlists(id, user_id, name, created, updated, source_id)
                         VALUES ('uproad', 1, 'Road Trip', 0, 0, 'plroad')""")
            c.executemany("INSERT INTO user_playlist_tracks(playlist_id, position, track_id, added_at) VALUES ('uproad', ?, ?, 0)",
                          [(0, tid(1)), (1, tid(3))])
            c.executemany("INSERT INTO track_sources(track_id, source) VALUES (?, 'up:uproad')", [(tid(1),), (tid(3),)])
        db.recompute_monitored()
        # song 1 is downloaded
        cls.song = cls.dir / "song1.m4a"
        shutil.copy(audio_file("m4a"), cls.song)
        cls.svc.db.set_status(f"{1:022d}", "downloaded", file_path=str(cls.song), file_size=cls.song.stat().st_size)
        cls.port = free_port()
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        code, _ = cls.svc.users.new_pairing_code(1)
        status, body, _ = cls.call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Test phone"})
        cls.token = body["token"]
        cls.kid = cls.svc.users.create("Kid")
        kcode, _ = cls.svc.users.new_pairing_code(cls.kid)
        cls.kid_token = cls.call(None, "POST", "/api/v1/auth/pair", {"code": kcode, "device": "Tablet"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_reordering_liked_songs(self):
        liked = lambda: [t["id"] for t in self.get("/api/v1/library/liked")[1]["tracks"]]  # noqa: E731
        before = liked()
        self.assertEqual(len(before), 2)
        st, _, _ = self.call(self.token, "POST", "/api/v1/library/liked/move", {"from": 1, "to": 0, "track_id": before[1]})
        self.assertEqual((st, liked()), (200, [before[1], before[0]]))
        self.assertEqual(self.call(self.token, "POST", "/api/v1/library/liked/move", {"from": 1, "to": 0, "track_id": before[1]})[0], 409)
        self.assertEqual(self.call(self.token, "POST", "/api/v1/library/liked/move", {"from": 0, "to": 5})[0], 400)
        # a new like goes first, ahead of the arrangement
        new = f"{3:022d}"
        self.call(self.token, "PUT", f"/api/v1/likes/{new}")
        self.assertEqual(liked(), [new, before[1], before[0]])
        self.call(self.token, "DELETE", f"/api/v1/likes/{new}")
        self.svc.db.run("DELETE FROM liked_order WHERE user_id = 1")
        self.assertEqual(liked(), before)  # back to newest like first

    def test_reordering_songs_in_a_playlist(self):
        pid = self.call(self.kid_token, "POST", "/api/v1/playlists", {"name": "Order"})[1]["id"]
        ids = [f"{n:022d}" for n in (1, 2, 3)]
        self.call(self.kid_token, "POST", f"/api/v1/playlists/{pid}/tracks", {"track_ids": ids})
        order = lambda: [t["id"] for t in self.call(self.kid_token, "GET", f"/api/v1/playlists/{pid}")[1]["tracks"]]  # noqa: E731
        st, _, _ = self.call(self.kid_token, "POST", f"/api/v1/playlists/{pid}/tracks/move", {"from": 2, "to": 0, "track_id": ids[2]})
        self.assertEqual((st, order()), (200, [ids[2], ids[0], ids[1]]))
        st, _, _ = self.call(self.kid_token, "POST", f"/api/v1/playlists/{pid}/tracks/move", {"from": 0, "to": 2, "track_id": ids[0]})
        self.assertEqual(st, 409)  # that's not where it is any more: nothing moves
        self.assertEqual(order(), [ids[2], ids[0], ids[1]])
        self.assertEqual(self.call(self.kid_token, "POST", f"/api/v1/playlists/{pid}/tracks/move", {"from": 0, "to": 9})[0], 400)
        self.assertEqual(self.call(self.token, "POST", f"/api/v1/playlists/{pid}/tracks/move", {"from": 0, "to": 1})[0], 404)  # not yours (and not told it exists)
        self.call(self.kid_token, "DELETE", f"/api/v1/playlists/{pid}")

    def test_arranging_playlists(self):
        made = [self.call(self.kid_token, "POST", "/api/v1/playlists", {"name": n})[1]["id"] for n in ("Road", "Gym", "Chill")]
        names = lambda lib: [p["name"] for p in lib["playlists"]]  # noqa: E731
        self.assertEqual(names(self.call(self.kid_token, "GET", "/api/v1/library")[1]), ["Chill", "Gym", "Road"])  # newest first
        status, lib, _ = self.call(self.kid_token, "PUT", "/api/v1/library/order", {"playlist_ids": [made[0], "nope", made[2], made[1]]})
        self.assertEqual((status, names(lib)), (200, ["Road", "Chill", "Gym"]))  # unknown ids are ignored
        self.call(self.kid_token, "POST", "/api/v1/playlists", {"name": "New one"})
        self.assertEqual(names(self.call(self.kid_token, "GET", "/api/v1/library")[1]), ["New one", "Road", "Chill", "Gym"])
        self.assertEqual(self.call(self.kid_token, "PUT", "/api/v1/library/order", {"playlist_ids": "Road"})[0], 400)
        for pid in made:
            self.call(self.kid_token, "DELETE", f"/api/v1/playlists/{pid}")

    def test_password_sign_in(self):
        status, acct, _ = self.call(self.kid_token, "GET", "/api/v1/account")
        self.assertEqual((status, acct["has_password"]), (200, False))
        status, acct, _ = self.call(self.kid_token, "POST", "/api/v1/account/password", {"login": "kiddo", "password": "short"})
        self.assertEqual(status, 400)
        status, acct, _ = self.call(self.kid_token, "POST", "/api/v1/account/password", {"login": "kiddo", "password": "long enough now"})
        self.assertEqual((status, acct["login"], acct["has_password"]), (200, "kiddo", True))
        status, body, _ = self.call(None, "POST", "/api/v1/auth/login", {"login": "Kiddo", "password": "long enough now", "device": "Laptop"})
        self.assertEqual((status, body["user"]["name"]), (200, "Kid"))
        self.assertEqual(self.call(body["token"], "GET", "/api/v1/account")[1]["login"], "kiddo")
        status, body, _ = self.call(None, "POST", "/api/v1/auth/login", {"login": "kiddo", "password": "nope nope"})
        self.assertEqual((status, "token" in body), (401, False))
        status, _, _ = self.call(self.kid_token, "POST", "/api/v1/account/password",
                                 {"login": "kiddo", "current": "wrong", "password": "something else"})
        self.assertEqual(status, 403)  # changing it needs the current one

    @classmethod
    def call(cls, token, method, path, body=None, headers=None, raw=False):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(cls.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                payload = r.read()
                return r.status, payload if raw else (json.loads(payload) if payload else None), r.headers
        except urllib.error.HTTPError as e:
            with e:
                payload = e.read()
                return e.code, payload if raw else (json.loads(payload) if payload else None), e.headers

    def get(self, path, token=None):
        return self.call(token or self.token, "GET", path)

    def test_nothing_public(self):
        for path in ("/", "/index.html", "/admin", "/api/status"):
            status, body, _ = self.call(None, "GET", path, raw=True)
            self.assertEqual((status, body), (404, b""), path)  # no page, not even an error page
        self.assertEqual(self.call(None, "GET", "/api/v1/me")[0], 401)
        self.assertEqual(self.call("bogus", "GET", "/api/v1/library")[0], 401)

    def test_me_and_library(self):
        me = self.get("/api/v1/me")[1]
        self.assertEqual((me["user"]["name"], me["spotify_accounts"]), ("Me", []))  # (still sent, empty, for older apps)
        lib = self.get("/api/v1/library")[1]
        self.assertEqual(lib["liked"]["count"], 2)
        # a playlist brought over from Spotify is an ordinary, editable playlist of yours
        road = [p for p in lib["playlists"] if p["name"] == "Road Trip"]
        self.assertEqual([(p["kind"], p["editable"], p["from_spotify"]) for p in road], [("app", True, True)])
        self.assertEqual(self.get("/api/v1/playlists/plroad")[1]["id"], road[0]["id"])  # older apps' links open it
        liked = self.get("/api/v1/library/liked")[1]
        self.assertEqual([t["id"] for t in liked["tracks"]], [f"{2:022d}", f"{1:022d}"])  # newest like first
        self.assertTrue(liked["tracks"][1]["playable"] and liked["tracks"][1]["liked"])
        # another profile sees none of it
        self.assertEqual(self.get("/api/v1/library", self.kid_token)[1]["liked"]["count"], 0)
        self.assertEqual(self.get("/api/v1/playlists/plroad", self.kid_token)[0], 404)

    def test_streaming_with_ranges(self):
        data = self.song.read_bytes()
        tid = f"{1:022d}"
        status, body, h = self.call(self.token, "GET", f"/api/v1/stream/{tid}", raw=True)
        self.assertEqual((status, body, h["Content-Type"], h["Accept-Ranges"]), (200, data, "audio/mp4", "bytes"))
        status, body, h = self.call(self.token, "GET", f"/api/v1/stream/{tid}", headers={"Range": "bytes=10-19"}, raw=True)
        self.assertEqual((status, body, h["Content-Range"]), (206, data[10:20], f"bytes 10-19/{len(data)}"))
        status, body, _ = self.call(self.token, "GET", f"/api/v1/stream/{tid}", headers={"Range": "bytes=-5"}, raw=True)
        self.assertEqual((status, body), (206, data[-5:]))
        status, body, _ = self.call(self.token, "GET", f"/api/v1/stream/{tid}", headers={"Range": f"bytes={len(data)}-"}, raw=True)
        self.assertEqual(status, 416)
        status, _, h = self.call(self.token, "GET", f"/api/v1/stream/{tid}", raw=True)
        status, body, _ = self.call(self.token, "GET", f"/api/v1/stream/{tid}", headers={"If-None-Match": h["ETag"]}, raw=True)
        self.assertEqual(status, 304)
        self.assertEqual(self.call(None, "GET", f"/api/v1/stream/{tid}", raw=True)[0], 401)  # never without a token
        self.assertEqual(self.get(f"/api/v1/stream/{2:022d}")[0], 404)  # not downloaded yet

    def test_missing_file_is_downloaded_again(self):
        t = spotify_track(9)
        self.svc.db.upsert_tracks([t])
        self.svc.db.set_status(t["id"], "downloaded", file_path=str(self.dir / "gone.m4a"))
        self.assertEqual(self.get(f"/api/v1/stream/{t['id']}")[0], 404)
        self.assertEqual(self.svc.db.one("SELECT status FROM tracks WHERE id = ?", (t["id"],))[0], "wanted")

    def test_likes(self):
        tid = f"{3:022d}"  # in Road Trip, not liked
        self.assertEqual(self.call(self.token, "PUT", f"/api/v1/likes/{tid}")[1], {"liked": True})
        self.assertIn(tid, [t["id"] for t in self.get("/api/v1/library/liked")[1]["tracks"]])
        self.assertEqual(self.call(self.token, "DELETE", f"/api/v1/likes/{tid}")[1]["liked"], False)
        # brought over from Spotify or not, a like is just a like
        self.assertEqual(self.call(self.token, "DELETE", f"/api/v1/likes/{1:022d}")[1], {"liked": False, "on_spotify": False})
        self.assertNotIn(f"{1:022d}", [t["id"] for t in self.get("/api/v1/library/liked")[1]["tracks"]])
        self.assertEqual(self.call(self.token, "PUT", f"/api/v1/likes/{1:022d}")[1], {"liked": True})
        self.assertIn(f"{1:022d}", [t["id"] for t in self.get("/api/v1/library/liked")[1]["tracks"]])
        self.svc.db.run("UPDATE likes SET created = 1000.0 WHERE user_id = 1 AND track_id = ?", (f"{1:022d}",))  # as it was

    def test_app_playlists(self):
        st, pl, _ = self.call(self.token, "POST", "/api/v1/playlists", {"name": "Gym"})
        pid = pl["id"]
        self.call(self.token, "POST", f"/api/v1/playlists/{pid}/tracks", {"track_ids": [f"{1:022d}", f"{3:022d}", "nope"]})
        got = self.get(f"/api/v1/playlists/{pid}")[1]
        self.assertEqual([t["id"] for t in got["tracks"]], [f"{1:022d}", f"{3:022d}"])
        self.call(self.token, "DELETE", f"/api/v1/playlists/{pid}/tracks/0")
        self.assertEqual([t["id"] for t in self.get(f"/api/v1/playlists/{pid}")[1]["tracks"]], [f"{3:022d}"])
        self.assertEqual(self.call(self.token, "PATCH", f"/api/v1/playlists/{pid}", {"name": "Gym 2"})[1]["name"], "Gym 2")
        self.assertEqual(self.get(f"/api/v1/playlists/{pid}", self.kid_token)[0], 404)  # private to its owner
        self.assertEqual(self.call(self.token, "POST", "/api/v1/playlists/plroad/tracks", {"track_ids": []})[0], 404)
        self.call(self.token, "DELETE", f"/api/v1/playlists/{pid}")
        self.assertEqual(self.get(f"/api/v1/playlists/{pid}")[0], 404)

    def test_plays_feed_home(self):
        self.call(self.token, "POST", "/api/v1/plays", {"track_id": f"{1:022d}", "ms_played": 60000, "completed": True})
        home = self.get("/api/v1/home")[1]
        recent = next(s for s in home["sections"] if s["id"] == "recently_played")
        self.assertEqual(recent["tracks"][0]["id"], f"{1:022d}")

    def test_search_library(self):
        res = self.get("/api/v1/search?q=Second")[1]
        self.assertEqual([t["title"] for t in res["tracks"]], ["Second Song"])

    def test_request_a_new_song(self):
        fake = {
            "/search/track?q=fresh%20tune&limit=20": {"data": [
                {"id": 21, "title": "Fresh Tune", "artist": {"name": "Artist 78"}, "album": {"title": "F"}, "duration": 200}]},
            "/track/21": {"id": 21, "title": "Fresh Tune", "isrc": "USNEW0000078", "artist": {"name": "Artist 78"},
                          "contributors": [{"name": "Artist 78"}], "album": {"id": 6, "title": "F"}, "duration": 200},
        }
        orig = discover._get
        discover._get = lambda path, ttl: fake[path]
        try:
            res = self.get("/api/v1/catalog/search?q=fresh%20tune")[1]
            self.assertEqual(res["source"], "deezer")
            hit = res["results"][0]
            self.assertEqual((hit["title"], hit["in_library"]), ("Fresh Tune", None))
            st, track, _ = self.call(self.token, "POST", "/api/v1/requests", {"source": "deezer", "id": hit["id"]})
            self.assertEqual((st, track["id"], track["status"]), (200, "dz21", "wanted"))
            row = self.svc.db.one("SELECT priority, monitored FROM tracks WHERE id = ?", ("dz21",))
            self.assertEqual(tuple(row), (REQUEST_PRIORITY, 1))
            claimed = self.svc.db.claim_next()
            self.assertEqual(claimed["priority"], REQUEST_PRIORITY)  # requests jump the backlog
            self.svc.db.set_status(claimed["id"], "wanted")
            again = self.get("/api/v1/catalog/search?q=fresh%20tune")[1]["results"][0]
            self.assertEqual(again["in_library"], "dz21")
            self.assertIn("dz21", [t["id"] for t in self.get("/api/v1/requests")[1]["requests"]])
            # an older app asking by a Spotify id: fine for a song the server knows, otherwise search again
            st, track, _ = self.call(self.token, "POST", "/api/v1/requests", {"source": "spotify", "id": f"{3:022d}"})
            self.assertEqual((st, track["id"]), (200, f"{3:022d}"))
            st, body, _ = self.call(self.token, "POST", "/api/v1/requests", {"source": "spotify", "id": "NotOnThisServer0000000"})
            self.assertEqual(st, 400)
            self.assertIn("Search for the song again", body["error"])
        finally:
            discover._get = orig

    def test_genre_charts_and_deezer_requests(self):
        fake = {
            "/genre": {"data": [{"id": 0, "name": "All"}, {"id": 152, "name": "Rock"}]},
            "/chart/152/tracks?limit=50": {"data": [
                {"id": 11, "title": "Second Song", "artist": {"name": "Artist 2"}, "album": {"title": "A"}, "duration": 180},
                {"id": 12, "title": "Brand New Hit", "artist": {"name": "Artist 2"}, "album": {"title": "B"}, "duration": 180},
            ]},
            "/track/12": {"id": 12, "title": "Brand New Hit", "isrc": "USNEW0000077", "artist": {"name": "Artist 2"},
                          "contributors": [{"name": "Artist 2"}], "album": {"id": 5, "title": "B"}, "duration": 180},
        }
        orig = discover._get
        discover._get = lambda path, ttl: fake[path]
        try:
            self.assertEqual([g["name"] for g in self.get("/api/v1/discover/genres")[1]["genres"]], ["All", "Rock"])
            chart = self.get("/api/v1/discover/genres/152")[1]["tracks"]
            self.assertEqual(chart[0]["in_library"], f"{2:022d}")  # already have it
            st, track, _ = self.call(self.token, "POST", "/api/v1/requests", {"source": "deezer", "id": 12})
            self.assertEqual(track["id"], f"{77:022d}")  # the song the server already has, found by its ISRC
        finally:
            discover._get = orig

    def test_app_updates(self):
        from songarr import app_updates
        app = self.dir / "fakeapp"
        out = app / "build" / "app" / "outputs" / "flutter-apk"
        out.mkdir(parents=True)
        (app / "pubspec.yaml").write_text("name: songarr_app\nversion: 1.2.0+7\n", encoding="utf-8")
        (out / "app-arm64-v8a-release.apk").write_bytes(b"PK\x03\x04 arm64 build 7")
        (out / "app-x86_64-release.apk").write_bytes(b"PK\x03\x04 x86 build 7")
        self.assertEqual(self.get("/api/v1/app/update?abi=arm64-v8a&build=1")[1], {"available": False, "update": None})
        app_updates.publish(self.svc.data_dir, app, "Faster start-up")
        st, res, _ = self.get("/api/v1/app/update?abi=arm64-v8a&build=1")
        self.assertTrue(res["available"])
        self.assertEqual((res["update"]["version"], res["update"]["build"], res["update"]["notes"]), ("1.2.0", 7, "Faster start-up"))
        self.assertFalse(self.get("/api/v1/app/update?abi=arm64-v8a&build=7")[1]["available"])  # already current
        self.assertFalse(self.get("/api/v1/app/update?abi=armeabi-v7a&build=1")[1]["available"])  # not built
        st, body, h = self.call(self.token, "GET", "/api/v1/app/update/apk?abi=arm64-v8a", raw=True)
        self.assertEqual((st, body, h["Content-Type"]), (200, b"PK\x03\x04 arm64 build 7", "application/vnd.android.package-archive"))
        import hashlib
        self.assertEqual(res["update"]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(self.call(None, "GET", "/api/v1/app/update/apk?abi=arm64-v8a")[0], 401)  # paired phones only
        self.assertEqual(self.get("/api/v1/app/update?abi=mips")[0], 400)
        # a newer build replaces the old file
        (app / "pubspec.yaml").write_text("name: songarr_app\nversion: 1.2.1+8\n", encoding="utf-8")
        (out / "app-arm64-v8a-release.apk").write_bytes(b"PK\x03\x04 arm64 build 8")
        app_updates.publish(self.svc.data_dir, app, "", ("arm64-v8a",))
        files = sorted(p.name for p in (self.svc.data_dir / "app-updates").iterdir())
        self.assertEqual(files, ["manifest.json", "songarr-1.2.0+7-x86_64.apk", "songarr-1.2.1+8-arm64-v8a.apk"])
        self.assertEqual(self.get("/api/v1/app/update?abi=arm64-v8a&build=7")[1]["update"]["build"], 8)

    def test_bad_requests(self):
        self.assertEqual(self.call(self.token, "POST", "/api/v1/requests", {"source": "nope"})[0], 400)
        self.assertEqual(self.call(self.token, "POST", "/api/v1/playlists", {"name": " "})[0], 400)
        self.assertEqual(self.call(self.token, "GET", "/api/v1/unknown")[0], 404)
        big = {"name": "x" * (70 * 1024)}
        self.assertEqual(self.call(self.token, "POST", "/api/v1/playlists", big)[0], 413)


class YouTubeSignInTests(unittest.TestCase):
    def test_netscape_format(self):
        from songarr.verify import netscape_cookies

        text = netscape_cookies([
            {"domain": ".youtube.com", "path": "/", "secure": True, "expires": 1900000000.5, "name": "SID", "value": "abc"},
            {"domain": "music.youtube.com", "path": "/", "secure": False, "expires": -1, "name": "PREF", "value": "x=1"},
        ])
        lines = text.splitlines()
        self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
        self.assertEqual(lines[2].split("\t"), [".youtube.com", "TRUE", "/", "TRUE", "1900000000", "SID", "abc"])
        self.assertEqual(lines[3].split("\t"), ["music.youtube.com", "FALSE", "/", "FALSE", "0", "PREF", "x=1"])

    def test_session_is_taken_from_the_sign_in_window(self):
        """A fake Chrome DevTools endpoint: /json/version over HTTP plus the DevTools websocket."""
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        from websockets.sync.server import serve

        from songarr.verify import YouTubeSignIn

        cookies = [{"domain": ".google.com", "path": "/", "name": "SID", "value": "g", "secure": True, "expires": -1}]
        calls = []
        port = free_port()

        def process_request(conn, request):
            if request.path == "/json/version":
                body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/x"}).encode()
                return Response(200, "OK", Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}), body)
            return None

        def handler(ws):
            for raw in ws:
                msg = json.loads(raw)
                calls.append(msg["method"])
                result = {"cookies": cookies} if msg["method"] == "Storage.getCookies" else {}
                ws.send(json.dumps({"id": msg["id"], "result": result}))

        server = serve(handler, "127.0.0.1", port, process_request=process_request)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        d = Path(tempfile.mkdtemp())
        try:
            s = YouTubeSignIn(d)

            class Proc:
                def poll(self):
                    return None

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    pass

            s.proc, s.port = Proc(), port
            with self.assertRaises(RuntimeError):  # only a Google cookie: not signed in to YouTube yet
                s.finish()
            cookies.append({"domain": ".youtube.com", "path": "/", "name": "LOGIN_INFO", "value": "yt", "secure": True, "expires": 2e9})
            out = s.finish()
            saved = Path(out["path"]).read_text(encoding="utf-8")
            self.assertIn("LOGIN_INFO\tyt", saved)
            self.assertNotIn(".google.com", saved)  # only YouTube's cookies are kept
            self.assertEqual(calls[-1], "Browser.close")
            self.assertFalse(s.window_open)
        finally:
            server.shutdown()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
