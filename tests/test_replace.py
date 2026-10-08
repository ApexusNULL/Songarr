"""Replacing a song that downloaded the wrong version (a cover, a live take): another YouTube upload,
chosen in the app (or on the admin site), downloaded over the old file. And the matcher no longer
takes a cover act with the artist's name in it for the artist. All offline."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from songarr.appapi import make_app_server
from songarr.matching import Candidate, TrackInfo, score
from songarr.service import Service

from tests import test_platform
from tests.helpers import FFMPEG, FakeYouTube, spotify_track


def wait_for(check, seconds: float = 15.0) -> bool:
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(FFMPEG, "FFmpeg not installed")
class ReplaceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.yt = FakeYouTube(delay=0.03)
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.yt)
        self.music = self.dir / "Music"
        self.svc.db.set_setting("library_root", str(self.music))
        self.svc.pacer.interval = lambda kind: 0.0
        self.me = self.svc.users.create("Alex")
        self.track = spotify_track(1, title="Radio Song")
        self.tid = self.track["id"]
        self.svc.db.upsert_tracks([self.track])
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.row()["status"] == "downloaded"))
        self.first = self.row()

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def row(self):
        return self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (self.tid,))

    def test_the_chosen_version_replaces_the_old_file(self):
        path = Path(self.first["file_path"])
        before = path.stat().st_mtime_ns
        time.sleep(0.05)
        self.svc.replace(self.tid, "rightone001", self.me)
        self.assertTrue(wait_for(lambda: self.row()["status"] == "downloaded" and self.row()["youtube_id"] == "rightone001"))
        after = self.row()
        self.assertEqual(after["file_path"], str(path))  # same place: the old file was written over
        self.assertNotEqual(path.stat().st_mtime_ns, before)
        self.assertIn("rightone001", self.yt.downloads)
        self.assertIsNone(after["previous_youtube_id"])
        said = self.svc.db.one("SELECT message FROM history WHERE event = 'manual-pick' AND track_id = ?", (self.tid,))[0]
        self.assertIn("Alex chose another version of Artist 1 - Radio Song", said)

    def test_the_old_file_somewhere_else_is_removed_if_songarr_made_it(self):
        old = Path(self.first["file_path"])
        elsewhere = self.music / "Elsewhere" / "Radio Song.m4a"
        elsewhere.parent.mkdir(parents=True)
        shutil.copy(old, elsewhere)  # (Songarr's tags: this song's id)
        old.unlink()
        self.svc.db.set_status(self.tid, "downloaded", file_path=str(elsewhere))
        self.svc.replace(self.tid, "rightone001", self.me)
        self.assertTrue(wait_for(lambda: self.row()["youtube_id"] == "rightone001" and self.row()["status"] == "downloaded"))
        self.assertFalse(elsewhere.exists())
        self.assertTrue(Path(self.row()["file_path"]).is_file())
        # someone's own file (not one of Songarr's) is never deleted
        mine = self.music / "Mine" / "song.m4a"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"my own file")
        self.svc.db.set_status(self.tid, "downloaded", file_path=str(mine))
        self.svc.replace(self.tid, "another0001", self.me)
        self.assertTrue(wait_for(lambda: self.row()["youtube_id"] == "another0001" and self.row()["status"] == "downloaded"))
        self.assertTrue(mine.exists())

    def test_a_version_that_wont_download_keeps_the_old_file(self):
        self.svc.replace(self.tid, "broken0000x", self.me)
        self.assertTrue(wait_for(lambda: self.row()["status"] == "downloaded" and self.row()["pinned"] == 0))
        kept = self.row()
        self.assertEqual((kept["file_path"], kept["youtube_id"]), (self.first["file_path"], self.first["youtube_id"]))
        self.assertTrue(Path(kept["file_path"]).is_file())
        self.assertIn("didn't download", self.svc.db.one(
            "SELECT message FROM history WHERE event = 'failed' AND track_id = ?", (self.tid,))[0])

    def test_through_the_app(self):
        port = test_platform.free_port()
        http = make_app_server(self.svc, "127.0.0.1", port)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        fake = type("Base", (), {"base": f"http://127.0.0.1:{port}"})
        call = lambda *a, **k: test_platform.AppAPITests.call.__func__(fake, *a, **k)  # noqa: E731
        try:
            code, _ = self.svc.users.new_pairing_code(self.me)
            token = call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"})[1]["token"]
            status, v, _ = call(token, "GET", f"/api/v1/tracks/{self.tid}/versions")
            self.assertEqual((status, v["current"], v["expected_s"]), (200, self.first["youtube_id"], 180))
            self.assertEqual([x["current"] for x in v["versions"]], [True, False])  # the one it has, then the live take
            self.assertTrue(v["versions"][1]["url"].startswith("https://youtu.be/"))
            searches = len([k for k, _ in self.yt.paced if k == "search"])
            status, v, _ = call(token, "GET", f"/api/v1/tracks/{self.tid}/versions?search=1")
            self.assertEqual((status, len([k for k, _ in self.yt.paced if k == "search"])), (200, searches + 1))  # searched again
            self.assertEqual(call(token, "POST", f"/api/v1/tracks/{self.tid}/replace", {"youtube": "not a link"})[0], 400)
            self.assertEqual(call(token, "POST", "/api/v1/tracks/NoSuchSong000000000000/replace", {"youtube": "rightone001"})[0], 404)
            status, t, _ = call(token, "POST", f"/api/v1/tracks/{self.tid}/replace",
                                {"youtube": "https://music.youtube.com/watch?v=rightone001&si=share"})
            self.assertEqual((status, t["status"]), (200, "wanted"))  # (replacing: downloading again)
            self.assertTrue(wait_for(lambda: self.row()["youtube_id"] == "rightone001" and self.row()["status"] == "downloaded"))
        finally:
            http.shutdown()
            http.server_close()


class CoverActTests(unittest.TestCase):
    """A cover act with the artist's name in it isn't the artist (the downloads this went wrong for)."""

    def scored(self, channel: str, artists: list[str], track: TrackInfo | None = None) -> Candidate:
        t = track or TrackInfo("Radio Ga Ga", ["Queen"], "The Works", 348)
        return score(t, Candidate(id="x", title=t.title, track=t.title, channel=f"{channel} - Topic", artists=artists,
                                  duration=t.duration, source="ytm"))

    def test_cover_acts_fall_below_the_bar(self):
        for act in ("Queen at The Opera Original Cast", "Queen Tribute Band", "The Queen Symphony Orchestra",
                    "Queen Karaoke Players", "Vitamin String Quartet plays Queen"):
            c = self.scored(act, [act])
            self.assertLess(c.score, 0.7, act)
            self.assertIn("cover act", " ".join(c.reasons), act)

    def test_the_artist_itself_is_fine(self):
        self.assertEqual(self.scored("Queen", ["Queen"]).score, 1.0)
        self.assertEqual(self.scored("Queen", ["Queen", "David Bowie"]).score, 1.0)
        lso = TrackInfo("Symphony No. 5", ["London Symphony Orchestra"], None, 400)
        self.assertEqual(self.scored("London Symphony Orchestra", ["London Symphony Orchestra"], lso).score, 1.0)
        cast = TrackInfo("Seasons of Love", ["Original Broadway Cast of Rent"], None, 180)
        self.assertGreater(self.scored("Original Broadway Cast of Rent", ["Original Broadway Cast of Rent"], cast).score, 0.9)


if __name__ == "__main__":
    unittest.main()
