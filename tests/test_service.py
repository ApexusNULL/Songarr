"""Service, playlist files and the admin website's API, all against local fakes (no network)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from songarr import playlists
from songarr.db import DB
from songarr.service import Service
from songarr.web import make_server

from tests.helpers import FFMPEG, FakeYouTube, spotify_track


def wait_for(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(FFMPEG, "FFmpeg not installed")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.yt = FakeYouTube()
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.yt)
        self.svc.db.set_setting("library_root", str(self.dir / "Music"))
        self.svc.pacer.interval = lambda kind: 0.0  # pacing has its own test

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self, tid):
        return self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))

    def test_deleting_a_songs_file(self):
        music = self.dir / "Music"
        self.svc.db.upsert_tracks([spotify_track(1), spotify_track(2)])
        for n in (1, 2):
            f = music / "Artist" / f"Album {n}" / f"0{n} - Song {n}.m4a"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"audio")
            self.svc.db.set_status(f"{n:022d}", "downloaded", file_path=str(f), file_size=5)
        self.svc.delete_file(f"{1:022d}")
        t = self.status(f"{1:022d}")
        self.assertEqual((t["status"], t["file_path"]), ("ignored", None))  # not downloaded straight back
        self.assertFalse((music / "Artist" / "Album 1").exists())  # empty album folder tidied
        self.assertTrue((music / "Artist" / "Album 2").exists())  # the artist still has an album
        self.svc.delete_file(f"{2:022d}", download_again=True)
        self.assertEqual(self.status(f"{2:022d}")["status"], "wanted")
        self.assertFalse((music / "Artist").exists())
        self.assertTrue(music.exists())  # never the library folder itself
        outside = self.dir / "elsewhere.m4a"
        outside.write_bytes(b"x")
        self.svc.db.set_status(f"{2:022d}", "downloaded", file_path=str(outside))
        with self.assertRaises(ValueError):
            self.svc.delete_file(f"{2:022d}")  # only files in the library folder
        self.assertTrue(outside.exists())

    def test_parallel_downloads_respect_worker_limit(self):
        self.svc.db.set_setting("workers", 3)
        self.svc.db.upsert_tracks([spotify_track(i) for i in range(1, 13)])
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.svc.db.counts()["downloaded"] == 12), self.svc.db.counts())
        self.assertEqual(self.yt.max_active, 3)  # really parallel, never above the limit
        for r in self.svc.db.q("SELECT * FROM tracks"):
            p = Path(r["file_path"])
            self.assertTrue(p.exists() and p.is_relative_to(self.dir / "Music"), p)
        hist = [r["event"] for r in self.svc.db.q("SELECT event FROM history")]
        self.assertEqual(hist.count("downloaded"), 12)

    def test_outcomes(self):
        tracks = {
            "ok": spotify_track(1, title="Fine Song"),
            "review": spotify_track(2, title="An obscure b-side"),
            "missing": spotify_track(3, title="missing song"),
        }
        self.svc.db.upsert_tracks(list(tracks.values()))
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.svc.db.one(
            "SELECT COUNT(*) FROM tracks WHERE status IN ('downloaded','review','failed')")[0] == 3))
        self.assertEqual(self.status(tracks["ok"]["id"])["status"], "downloaded")
        rv = self.status(tracks["review"]["id"])
        self.assertEqual(rv["status"], "review")
        self.assertEqual(len(self.svc.db.q("SELECT * FROM candidates WHERE track_id = ?", (rv["id"],))), 1)
        miss = self.status(tracks["missing"]["id"])
        self.assertEqual((miss["status"], miss["attempts"]), ("failed", 1))
        self.assertAlmostEqual(miss["next_attempt"] - time.time(), 3600, delta=30)  # retried in an hour

        # picking an upload by hand downloads exactly that upload
        self.svc.pick(rv["id"], "handpicked1")
        self.assertTrue(wait_for(lambda: self.status(rv["id"])["status"] == "downloaded"))
        self.assertEqual(self.status(rv["id"])["youtube_id"], "handpicked1")
        self.assertIn("handpicked1", self.yt.downloads)

    def test_bot_check_pauses_everything_and_survives_restart(self):
        t = spotify_track(9, title="bot trap")  # newest like: processed first
        self.svc.db.upsert_tracks([t, spotify_track(2)])
        self.svc.db.set_setting("workers", 1)
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.svc.cooldown_until > time.time()))
        self.assertEqual(self.status(t["id"])["status"], "wanted")  # not counted as a failure
        self.assertAlmostEqual(self.svc.cooldown_until - time.time(), 30 * 60, delta=5)
        self.assertEqual(self.svc.rate_factor, 0.5)
        time.sleep(0.5)
        self.assertEqual(self.svc.db.counts()["downloaded"], 0)  # nothing else starts during the cooldown
        # a restart must not resume hammering YouTube
        again = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.yt)
        self.assertAlmostEqual(again.cooldown_until, self.svc.cooldown_until, delta=0.01)
        self.assertEqual((again.rate_factor, again.blocks), (0.5, 1))
        again.db.close()

    def test_consecutive_blocks_back_off_exponentially_and_recover(self):
        from songarr.youtube import Blocked

        t = spotify_track(1)
        self.svc.db.upsert_tracks([t])
        pauses = []
        for _ in range(4):
            self.svc.cooldown_until = 0
            self.svc._on_blocked(Blocked("bot", "x"), dict(t))
            pauses.append(round((self.svc.cooldown_until - time.time()) / 60))
        self.assertEqual(pauses, [30, 60, 120, 240])
        self.assertEqual(self.svc.rate_factor, 0.25)  # floor
        for _ in range(40):
            self.svc._on_success()
        self.assertEqual(self.svc.blocks, 0)
        self.assertGreater(self.svc.rate_factor, 0.25)  # pace recovers as downloads succeed

    def test_repeated_403_requeues_then_fails(self):
        t = spotify_track(1, title="forbidden fruit")
        self.svc.db.upsert_tracks([t])
        for expected in ("wanted", "wanted", "failed"):
            self.svc.cooldown_until = 0
            from songarr.db import track_dict

            self.svc._run(track_dict(self.svc.db.claim_next()))
            self.assertEqual(self.status(t["id"])["status"], expected)
        self.assertIn("403", self.status(t["id"])["error"])

    def test_sign_in_only_songs_wait_for_a_sign_in(self):
        from songarr.service import NEEDS_SIGNIN
        from songarr.db import track_dict

        t = spotify_track(1, title="agegate anthem")
        self.svc.db.upsert_tracks([t])
        self.svc._run(track_dict(self.svc.db.claim_next()))
        row = self.status(t["id"])
        self.assertEqual((row["status"], row["error"], row["next_attempt"]), ("failed", NEEDS_SIGNIN, None))
        self.assertEqual(self.svc.cooldown_until, 0)  # not treated as throttling
        self.assertEqual(self.svc.retry_needing_signin(), 1)  # what signing in to YouTube triggers
        self.assertEqual(self.status(t["id"])["status"], "wanted")

    def test_same_recording_under_two_spotify_ids_is_downloaded_once(self):
        from songarr.db import track_dict

        a = spotify_track(1, title="Glimpse of Us", isrc="USWB12201789", album="Glimpse of Us", album_id="al1")
        b = spotify_track(2, title="Glimpse of Us", isrc="USWB12201789", album="Glimpse of Us", album_id="al2",
                          artists=a["artists"], album_artists=a["album_artists"], track_number=1)
        a["track_number"] = 1
        c = spotify_track(3, title="Glimpse of Us", isrc="USWB12201789", album="Deluxe Edition", album_id="al3",
                          artists=a["artists"], album_artists=a["album_artists"])
        self.svc.db.upsert_tracks([a, b, c])
        for _ in range(3):
            self.svc._run(track_dict(self.svc.db.claim_next()))
        rows = {r["id"]: r for r in self.svc.db.q("SELECT * FROM tracks")}
        self.assertEqual({r["status"] for r in rows.values()}, {"downloaded"})
        self.assertEqual(len(self.yt.downloads), 1)  # YouTube asked once for three Spotify entries
        self.assertEqual(rows[a["id"]]["file_path"], rows[b["id"]]["file_path"])  # identical entry: one shared file
        self.assertNotEqual(rows[c["id"]]["file_path"], rows[a["id"]]["file_path"])  # other album: its own tagged copy
        self.assertIn("Deluxe Edition", rows[c["id"]]["file_path"])
        from songarr import tagging

        self.assertEqual(tagging.read_spotify_id(Path(rows[c["id"]]["file_path"])), c["id"])

    def test_untitled_song_fails_without_searching(self):
        from songarr.db import track_dict

        t = spotify_track(1, title="")
        self.svc.db.upsert_tracks([t])
        self.svc._run(track_dict(self.svc.db.claim_next()))
        row = self.status(t["id"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("without a title", row["error"])

    def test_restart_does_not_delete_a_running_download(self):
        self.svc.start()
        busy = self.svc.tmp / "sometrack"
        busy.mkdir(parents=True)
        (busy / "part.webm").write_bytes(b"x")
        stale = self.svc.tmp_root / "run-1-1"
        stale.mkdir(parents=True)
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        again = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.yt)
        again.pacer.interval = lambda kind: 0.0
        again.start()
        try:
            self.assertTrue((busy / "part.webm").exists())  # the previous run may still be writing
            self.assertFalse(stale.exists())  # an hour-old leftover is cleaned up
            self.assertNotEqual(again.tmp, self.svc.tmp)
        finally:
            again.stop(wait=True)

    def test_pacing_spaces_requests_across_workers(self):
        self.svc.pacer.interval = lambda kind: 0.25 if kind == "download" else 0.0
        self.svc.db.set_setting("workers", 4)
        self.svc.db.upsert_tracks([spotify_track(i) for i in range(1, 7)])
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.svc.db.counts()["downloaded"] == 6))
        starts = sorted(t for kind, t in self.yt.paced if kind == "download")
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreaterEqual(min(gaps), 0.25 * 0.85 - 0.02, gaps)  # 4 workers, still one download per interval

    def test_existing_file_is_recognised_not_redownloaded(self):
        t = spotify_track(4)
        self.svc.db.upsert_tracks([t])
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.status(t["id"])["status"] == "downloaded"))
        self.svc.retry(t["id"])
        self.assertTrue(wait_for(lambda: self.svc.db.one("SELECT COUNT(*) FROM history WHERE event = 'imported'")[0] == 1))
        self.assertEqual(len(self.yt.downloads), 1)

    def test_restart_requeues_interrupted_work(self):
        t = spotify_track(1)
        self.svc.db.upsert_tracks([t])
        self.svc.db.set_status(t["id"], "downloading")
        self.assertEqual(self.svc.db.reset_busy(), 1)
        self.assertEqual(self.status(t["id"])["status"], "wanted")


class PlaylistFileTests(unittest.TestCase):
    """<library>/Playlists: everyone's Liked Songs and playlists, as .m3u8 files for other players."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = DB(self.dir / "songarr.db")
        self.lib = self.dir / "Music"
        self.folder = self.lib / "Playlists"
        self.sam = self.db.run_insert("INSERT INTO users(name, is_admin, created) VALUES ('Sam', 0, 0)")
        self.db.upsert_tracks([spotify_track(n) for n in (1, 2, 3)])
        for n in (1, 2, 3):  # all three are downloaded
            f = self.lib / "Artist" / "Album" / f"{self.tid(n)}.m4a"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            self.db.set_status(self.tid(n), "downloaded", file_path=str(f))
        with self.db.tx() as c:
            c.executemany("INSERT INTO likes VALUES (?, ?, ?)", [(1, self.tid(1), 1.0), (1, self.tid(2), 2.0), (self.sam, self.tid(3), 1.0)])
            c.executemany("INSERT INTO user_playlists(id, user_id, name, created, updated) VALUES (?, ?, ?, 0, 0)",
                          [("upme", 1, "Road Trip"), ("upsam", self.sam, "Road Trip"), ("upgym", self.sam, "Gym")])
            c.executemany("INSERT INTO user_playlist_tracks VALUES (?, ?, ?, 0)",
                          [("upme", 0, self.tid(3)), ("upme", 1, self.tid(1)), ("upsam", 0, self.tid(2)), ("upgym", 0, self.tid(1))])

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def tid(n: int) -> str:
        return f"{n:022d}"

    def songs(self, name: str) -> list[str]:
        lines = (self.folder / name).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        return [line for line in lines if not line.startswith("#")]

    def names(self) -> list[str]:
        return sorted(p.name for p in self.folder.glob("*.m3u8"))

    def test_everyones_lists(self):
        playlists.write_all(self.db, str(self.lib))
        self.assertEqual(self.names(), ["Gym.m3u8", "Liked Songs (Me).m3u8", "Liked Songs (Sam).m3u8",
                                        "Road Trip (Me).m3u8", "Road Trip (Sam).m3u8"])  # names say whose when needed
        self.assertEqual(self.songs("Road Trip (Me).m3u8"),
                         [f"../Artist/Album/{self.tid(3)}.m4a", f"../Artist/Album/{self.tid(1)}.m4a"])  # relative, in order
        self.assertEqual(self.songs("Liked Songs (Me).m3u8"),
                         [f"../Artist/Album/{self.tid(2)}.m4a", f"../Artist/Album/{self.tid(1)}.m4a"])  # newest like first

    def test_changes_and_tidying_up(self):
        playlists.write_all(self.db, str(self.lib))
        (self.folder / "Mine.m3u8").write_text("#EXTM3U\n", encoding="utf-8")  # not Songarr's: never touched
        self.db.run("DELETE FROM likes WHERE user_id = ?", (self.sam,))  # one person likes songs: plain "Liked Songs"
        self.db.run("UPDATE user_playlists SET name = 'Road Trip 2026' WHERE id = 'upme'")  # renamed
        self.db.run("DELETE FROM user_playlists WHERE id = 'upgym'")  # deleted
        playlists.write_all(self.db, str(self.lib))
        self.assertEqual(self.names(), ["Liked Songs.m3u8", "Mine.m3u8", "Road Trip 2026.m3u8", "Road Trip.m3u8"])
        self.assertTrue((self.lib / "Artist" / "Album" / f"{self.tid(1)}.m4a").exists())  # songs on disk stay

    def test_a_playlist_called_liked_songs(self):
        self.db.run("UPDATE user_playlists SET name = 'Liked Songs' WHERE id IN ('upme', 'upgym')")
        playlists.write_all(self.db, str(self.lib))
        self.assertEqual(self.names(), ["Liked Songs (Me).m3u8", "Liked Songs (Sam).m3u8", "Liked Songs playlist (Me).m3u8",
                                        "Liked Songs playlist (Sam).m3u8", "Road Trip.m3u8"])

    def test_files_left_from_spotify_are_tidied_up(self):
        self.folder.mkdir(parents=True)
        (self.folder / "Top Hits.m3u8").write_text("#EXTM3U\n#PLAYLIST:Top Hits\n", encoding="utf-8")
        self.db.set_setting("m3u_files", ["Top Hits.m3u8"])  # recorded when Spotify was retired
        playlists.write_all(self.db, str(self.lib))
        self.assertNotIn("Top Hits.m3u8", self.names())


class SearchTests(unittest.TestCase):
    def test_youtube_music_results_are_filtered_and_mapped(self):
        from songarr.youtube import Blocked, YouTube

        class FakeYTM:
            def __init__(self, results=None, error=None):
                self.results, self.error = results, error

            def search(self, q, filter=None, limit=None):
                if self.error:
                    raise self.error
                return self.results

        yt = YouTube(js_runtimes={})
        yt._local.ytm = FakeYTM([
            {"resultType": "song", "videoId": "NPdgPZ0u3zQ", "title": "Monkeys", "duration_seconds": 125,
             "artists": [{"name": "Kevin MacLeod"}], "album": {"name": "Monkeys"}},
            {"resultType": "playlist", "videoId": None, "title": "Mix"},
            {"resultType": "song", "videoId": "RDAMVM0X2mn&list", "title": "Radio mix"},
        ])
        out = yt.search_music("q")
        self.assertEqual([(c.id, c.duration, c.artists, c.album) for c in out],
                         [("NPdgPZ0u3zQ", 125, ["Kevin MacLeod"], "Monkeys")])
        yt._local.ytm = FakeYTM(error=Exception("Server returned HTTP 429: Too Many Requests."))
        with self.assertRaises(Blocked):
            yt.search_music("q")

    def test_error_classification(self):
        from songarr.youtube import Blocked, Unavailable, classify

        self.assertEqual(classify(Exception("Sign in to confirm you’re not a bot")).kind, "bot")
        self.assertEqual(classify(Exception("HTTP Error 429: Too Many Requests")).kind, "bot")
        self.assertEqual(classify(Exception("unable to download video data: HTTP Error 403: Forbidden")).kind, "throttle")
        self.assertIsInstance(classify(Exception("[youtube] RDAMVM0X2mn: This video is unavailable")), Unavailable)
        self.assertNotIsInstance(classify(Exception("something else")), Blocked)
        from songarr.youtube import NeedsSignIn

        self.assertIsInstance(classify(Exception("Sign in to confirm your age. Use --cookies")), NeedsSignIn)
        self.assertIsInstance(classify(Exception("[youtube] K2tt9j7Bygs: Please sign in. Use --cookies")), NeedsSignIn)


class RetryReviewTests(unittest.TestCase):
    """Wanted → Needs review → Retry all: every song there is searched for again."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.svc = Service(self.dir, self.port, youtube_factory=lambda s: FakeYouTube())
        self.svc.db.upsert_tracks([spotify_track(n) for n in range(1, 5)])
        for n in (1, 2):
            self.svc.db.set_status(f"{n:022d}", "review", match_score=0.4, error="Best match only scored 0.40", pinned=1)
        self.svc.db.set_status(f"{3:022d}", "failed", error="no YouTube uploads found")
        self.http = make_server(self.svc, "127.0.0.1", self.port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_retry_all_in_needs_review(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/retry-review", data=b"{}", method="POST",
                                     headers={"X-Songarr": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.loads(r.read())["count"], 2)
        rows = {r["id"][-1]: dict(r) for r in self.svc.db.q("SELECT id, status, error, pinned FROM tracks")}
        self.assertEqual({k: v["status"] for k, v in rows.items()}, {"1": "wanted", "2": "wanted", "3": "failed", "4": "wanted"})
        self.assertIsNone(rows["1"]["error"])
        self.assertEqual(rows["1"]["pinned"], 0)  # searched for afresh, not the old pick
        self.assertIn("2 songs that needed review", self.svc.db.one("SELECT message FROM history WHERE event = 'retry'")[0])
        self.assertEqual(self.svc.retry_review(), 0)  # nothing left there


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.svc = Service(cls.dir, cls.port, youtube_factory=lambda s: FakeYouTube())
        cls.svc.db.upsert_tracks([spotify_track(1), spotify_track(2, title="<script>alert(1)</script>")])
        cls.http = make_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def req(self, path, body=None, headers=None, follow=True):
        h = {"X-Songarr": "1", "Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def test_ui_and_status(self):
        code, _, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Songarr", body)
        code, _, body = self.req("/api/status")
        st = json.loads(body)
        self.assertEqual(st["counts"]["wanted"], 2)
        self.assertEqual(st["people"], {"count": 1, "likes": 0})

    def test_tracks_listing_and_search(self):
        data = json.loads(self.req("/api/tracks?q=script")[2])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["title"], "<script>alert(1)</script>")  # raw in JSON; the UI escapes it

    def test_post_requires_header_and_rejects_foreign_hosts(self):
        r = urllib.request.Request(self.base + "/api/queue/pause", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r)
        e.exception.close()
        self.assertEqual(e.exception.code, 403)  # a cross-site form post can't set X-Songarr
        code, _, _ = self.req("/api/status", headers={"Host": "evil.example:8484"})
        self.assertEqual(code, 403)  # DNS rebinding

    def test_settings_are_validated(self):
        self.req("/api/settings", {"workers": 99, "audio_format": "wav", "match_threshold": "0.8", "nonsense": 1})
        s = json.loads(self.req("/api/settings")[2])
        self.assertEqual((s["workers"], s["audio_format"], s["match_threshold"]), (8, "m4a", 0.8))
        self.assertNotIn("nonsense", s)
        self.assertNotIn("spotify_client_id", s)  # Songarr no longer reads Spotify
        self.req("/api/settings", {"workers": 4})

    def test_pick_accepts_links(self):
        tid = f"{1:022d}"
        code, _, body = self.req(f"/api/tracks/{tid}/pick", {"youtube": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(code, 200, body)
        row = self.svc.db.one("SELECT youtube_id, pinned, status FROM tracks WHERE id = ?", (tid,))
        self.assertEqual(tuple(row), ("dQw4w9WgXcQ", 1, "wanted"))
        code, _, _ = self.req(f"/api/tracks/{tid}/pick", {"youtube": "hello"})
        self.assertEqual(code, 400)

    def test_spotify_sign_in_is_gone(self):
        for path in ("/connect", "/callback?code=x&state=y"):
            self.assertEqual(self.req(path, follow=False)[0], 404)
        self.assertEqual(self.req("/api/sync", {})[0], 404)

    def test_playlists_and_library_filter(self):
        db, t1, t2 = self.svc.db, f"{1:022d}", f"{2:022d}"
        db.run("INSERT INTO likes VALUES (1, ?, 0)", (t1,))
        db.add_source(t1, "like:1")
        db.run("INSERT INTO user_playlists(id, user_id, name, created, updated) VALUES ('upweb', 1, 'Web Mix', 0, 0)")
        db.run("INSERT INTO user_playlist_tracks VALUES ('upweb', 0, ?, 0)", (t2,))
        db.add_source(t2, "up:upweb")
        try:
            pls = json.loads(self.req("/api/playlists")[2])
            self.assertEqual([(p["id"], p["name"], p["owner"], p["songs"]) for p in pls],
                             [("like:1", "Liked Songs", "Me", 1), ("up:upweb", "Web Mix", "Me", 1)])
            for pl, want in (("like:1", t1), ("up:upweb", t2)):
                data = json.loads(self.req(f"/api/tracks?playlist={urllib.parse.quote(pl)}")[2])
                self.assertEqual([t["id"] for t in data["items"]], [want])
        finally:
            db.run("DELETE FROM likes")
            db.run("DELETE FROM user_playlist_tracks")
            db.run("DELETE FROM user_playlists")
            db.run("DELETE FROM track_sources")


class ShutdownTest(unittest.TestCase):
    def test_shutdown_button_stops_the_server(self):
        d = Path(tempfile.mkdtemp())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        svc = Service(d, port, youtube_factory=lambda s: FakeYouTube())
        http = make_server(svc, "127.0.0.1", port)
        t = threading.Thread(target=http.serve_forever, daemon=True)
        t.start()
        r = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown", data=b"{}",
                                   headers={"X-Songarr": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            self.assertEqual(resp.status, 200)
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        http.server_close()
        svc.stop(wait=True)
        shutil.rmtree(d, ignore_errors=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


if __name__ == "__main__":
    unittest.main()
