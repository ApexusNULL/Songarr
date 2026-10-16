"""Smart Shuffle (songs a profile is likely to love next, mixed into a list it shuffles) and songs on
trial (downloaded to try out, kept for good once the family's listening says so). All offline."""

from __future__ import annotations

import random
import shutil
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from songarr import discover, smartshuffle
from songarr.appapi import make_app_server
from songarr.service import Service

from tests import test_platform
from tests.helpers import FFMPEG, FakeYouTube, spotify_track


def wait_for(check, seconds: float = 15.0) -> bool:
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        time.sleep(0.05)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube(delay=0.03))
        self.music = self.dir / "Music"
        self.svc.db.set_setting("library_root", str(self.music))
        self.svc.pacer.interval = lambda kind: 0.0
        self.me = self.svc.users.create("Alex")
        self.sam = self.svc.users.create("Sam")
        self.kim = self.svc.users.create("Kim")

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def song(self, n: int, artist: str, title: str | None = None, year: str = "2020-01-02", on_server: bool = True) -> str:
        t = spotify_track(n, title=title or f"Song {n}", artists=[artist], album_artists=[artist], album=f"{artist} album",
                          release_date=year)
        self.svc.db.upsert_tracks([t])
        if on_server:
            self.svc.db.set_status(t["id"], "downloaded", file_path=str(self.music / f"{n}.m4a"))
        return t["id"]

    def like(self, uid: int, tid: str) -> None:
        self.svc.db.run("INSERT INTO likes(user_id, track_id, created) VALUES (?, ?, ?)", (uid, tid, time.time()))
        self.svc.db.add_source(tid, f"like:{uid}")

    def finished(self, uid: int, tid: str, times: int = 1) -> None:
        for _ in range(times):
            self.svc.db.run("INSERT INTO plays(user_id, track_id, played_at, ms_played, completed) VALUES (?,?,?,?,1)",
                            (uid, tid, time.time() - 3600 * 24 * 5, 180_000))


class SuggestTests(Base):
    def setUp(self):
        super().setUp()
        # Alex's Liked Songs: Queen and the Beatles, from the 70s and 80s
        self.liked = [self.song(1, "Queen", year="1984-02-27"), self.song(2, "Queen", year="1975-10-31"),
                      self.song(3, "The Beatles", year="1969-09-26")]
        for t in self.liked:
            self.like(self.me, t)
        self.more_queen = self.song(10, "Queen", year="1980-06-30")  # on the server, not in the list
        self.noise = [self.song(20 + i, f"Noise Band {i}", year="2024-01-01") for i in range(6)]  # nothing in common
        self.skipped = self.song(30, "Skippy")
        self.svc.smart_shuffle.skipped(self.me, self.skipped, 4000)
        # Sam's taste is like Alex's, and Sam plays this one to the end a lot
        self.like(self.sam, self.song(40, "Queen", year="1977-10-28"))
        self.sams = self.song(41, "Freddie Mercury", year="1985-04-29")
        self.finished(self.sam, self.sams, 3)

    def draws(self, n: int = 3, runs: int = 60) -> Counter:
        seen: Counter = Counter()
        for i in range(runs):
            for tid, _ in self.svc.smart_shuffle.suggest(self.me, self.liked, n, rng=random.Random(i)):
                seen[tid] += 1
        return seen

    def test_what_fits_beats_what_doesnt(self):
        seen = self.draws()
        self.assertGreater(seen[self.more_queen], max(seen[t] for t in self.noise))
        self.assertGreater(seen[self.sams], max(seen[t] for t in self.noise))  # the family's favourite, by taste
        self.assertEqual(seen[self.skipped], 0)  # skipped before
        self.assertFalse(set(seen) & set(self.liked))  # never what's in the list (or already liked)

    def test_why_each_was_picked(self):
        why = {}
        for i in range(40):
            why.update(self.svc.smart_shuffle.suggest(self.me, self.liked, 4, rng=random.Random(i)))
        self.assertEqual(why[self.more_queen], "More from Queen")
        self.assertEqual(why[self.sams], "Loved by Sam")

    def test_no_more_than_two_by_one_artist(self):
        for i in range(10):
            self.song(100 + i, "Queen", year="1980-01-01")
        picks = self.svc.smart_shuffle.suggest(self.me, self.liked, 6, rng=random.Random(3))
        artists = Counter(self.svc.db.one("SELECT artists FROM tracks WHERE id = ?", (tid,))[0] for tid, _ in picks)
        self.assertLessEqual(max(artists.values()), 2)

    def test_skipping_an_artist_counts_against_them(self):
        for i in range(4):
            self.svc.smart_shuffle.skipped(self.me, self.song(50 + i, "Queen", year="1981-01-01"), 3000)
        self.assertLess(self.draws()[self.more_queen], 60)  # not always on top any more


class TrialTests(Base):
    def setUp(self):
        super().setUp()
        self.found = {}  # Deezer, made up
        self.orig_full_track = discover.full_track
        discover.full_track = lambda i: self.found[int(i)]
        self.picks = []
        self.svc.recommender.get = lambda uid: {"songs": self.picks, "artists": []}

    def tearDown(self):
        discover.full_track = self.orig_full_track
        super().tearDown()

    def suggest(self, n: int, title: str, artist: str = "New Artist") -> str:
        t = spotify_track(n, title=title, artists=[artist], album_artists=[artist], album="New Album")
        self.found[n] = t
        self.picks.append({"source": "deezer", "id": str(n), "title": title, "artists": [artist]})
        return t["id"]

    def row(self, tid: str):
        return self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))

    def test_only_for_people_using_smart_shuffle(self):
        self.suggest(1, "Tried Song")
        self.svc.smart_shuffle.top_up()
        self.assertIsNone(self.row(f"{1:022d}"))
        self.svc.smart_shuffle.used(self.me)
        self.svc.smart_shuffle.top_up()
        row = self.row(f"{1:022d}")
        self.assertEqual((row["trial"], row["monitored"], row["priority"]), (1, 1, smartshuffle.TRIAL_PRIORITY))

    def test_a_song_already_on_the_server_isnt_put_on_trial(self):
        self.song(5, "New Artist", title="Tried Song")
        self.suggest(6, "Tried Song")
        self.svc.smart_shuffle.used(self.me)
        self.svc.smart_shuffle.top_up()
        self.assertIsNone(self.row(f"{6:022d}"))

    @unittest.skipUnless(FFMPEG, "FFmpeg not installed")
    def test_kept_when_liked_moved_into_the_music_folder(self):
        tid = self.suggest(1, "Tried Song")
        self.svc.smart_shuffle.used(self.me)
        self.svc.smart_shuffle.top_up()
        self.svc.start()
        self.svc.wake.set()
        self.assertTrue(wait_for(lambda: self.row(tid)["status"] == "downloaded"))
        trial = self.music / smartshuffle.TRIAL_FOLDER
        path = Path(self.row(tid)["file_path"])
        self.assertTrue(path.is_relative_to(trial) and path.is_file())
        self.assertTrue((trial / ".plexignore").is_file() and (trial / ".ignore").is_file())  # media servers skip it
        self.svc.scanner.scan()  # and so does the music folder's own index
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM library_files WHERE path LIKE ?", (f"%{smartshuffle.TRIAL_FOLDER}%",))[0], 0)
        self.svc.smart_shuffle.judge()
        self.assertEqual(self.row(tid)["trial"], 1)  # nothing yet
        self.like(self.sam, tid)
        self.svc.smart_shuffle.judge()
        row = self.row(tid)
        kept = Path(row["file_path"])
        self.assertEqual(row["trial"], 0)
        self.assertTrue(kept.is_file() and not kept.is_relative_to(trial) and kept.is_relative_to(self.music))
        self.assertFalse(path.exists())
        self.assertIn("stays for good: liked by Sam", self.svc.db.one("SELECT message FROM history WHERE event = 'kept'")[0])

    def test_kept_when_played_through_enough(self):
        tid = self.suggest(1, "Tried Song")
        self.svc.smart_shuffle.used(self.me)
        self.svc.smart_shuffle.top_up()
        self.finished(self.me, tid, 3)
        self.svc.smart_shuffle.judge()
        self.assertIsNone(self.svc.smart_shuffle.keeps(tid))  # 3 plays, by one person
        self.finished(self.kim, tid)
        for _ in range(4):  # but skipped as often as played through
            self.svc.smart_shuffle.skipped(self.sam, tid, 2000)
        self.assertIsNone(self.svc.smart_shuffle.keeps(tid))
        self.finished(self.kim, tid)
        self.assertEqual(self.svc.smart_shuffle.keeps(tid), "played to the end 5 times by 2 people")
        self.svc.smart_shuffle.judge()
        self.assertEqual(self.row(tid)["trial"], 0)

    def test_let_go_after_its_trial(self):
        tid = self.suggest(1, "Tried Song")
        self.svc.smart_shuffle.used(self.me)
        self.svc.smart_shuffle.top_up()
        trial = smartshuffle.trial_root(self.music)
        f = trial / "New Artist" / "New Album" / "01 - Tried Song.m4a"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"audio")
        self.svc.db.set_status(tid, "downloaded", file_path=str(f), trial_since=time.time() - 31 * 86400)
        self.svc.smart_shuffle.judge()
        row = self.row(tid)
        self.assertEqual((row["trial"], row["monitored"], row["file_path"]), (2, 0, None))
        self.assertFalse(f.exists() or f.parent.exists())  # the empty folders go too
        self.svc.smart_shuffle.top_up()  # and it isn't put on trial again
        self.assertEqual(self.row(tid)["trial"], 2)

    def test_a_day_has_a_limit(self):
        self.svc.smart_shuffle.used(self.me)
        for n in range(1, smartshuffle.PER_DAY + 10):
            self.suggest(n, f"Tried {n}", artist=f"Artist {n}")
        for uid in (self.sam, self.kim):
            self.svc.smart_shuffle.used(uid)
        self.svc.smart_shuffle.top_up()
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM tracks WHERE trial = 1")[0], 3 * smartshuffle.READY)
        smartshuffle.READY, ready = 100, smartshuffle.READY
        try:
            self.svc.smart_shuffle.top_up()
            self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM tracks WHERE trial = 1")[0], smartshuffle.PER_DAY)
        finally:
            smartshuffle.READY = ready


class AppTests(Base):
    def test_through_the_app(self):
        liked = [self.song(1, "Queen"), self.song(2, "Queen")]
        for t in liked:
            self.like(self.me, t)
        other = self.song(3, "Queen")
        port = test_platform.free_port()
        http = make_app_server(self.svc, "127.0.0.1", port)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        fake = type("Base", (), {"base": f"http://127.0.0.1:{port}"})
        call = lambda *a, **k: test_platform.AppAPITests.call.__func__(fake, *a, **k)  # noqa: E731
        code, _ = self.svc.users.new_pairing_code(self.me)
        token = call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"})[1]["token"]
        status, res, _ = call(token, "POST", "/api/v1/smart-shuffle", {"list": "liked", "n": 5})
        self.assertEqual(status, 200)
        self.assertEqual([(t["id"], t["suggested"], t["reason"]) for t in res["tracks"]], [(other, True, "More from Queen")])
        self.assertEqual(call(token, "POST", "/api/v1/smart-shuffle", {"list": "liked", "not": [other]})[1]["tracks"], [])
        self.assertIsNotNone(self.svc.db.one("SELECT used FROM smart_shuffle WHERE user_id = ?", (self.me,)))
        self.assertEqual(call(token, "POST", "/api/v1/smart-shuffle", {"list": "upnotmine0000001"})[0], 404)
        self.assertEqual(call(token, "POST", "/api/v1/skips", {"track_id": other, "ms_played": 4200})[1], {"ok": True})
        self.assertEqual(self.svc.db.one("SELECT ms_played FROM skips WHERE user_id = ? AND track_id = ?", (self.me, other))[0], 4200)
        self.assertEqual(call(token, "POST", "/api/v1/skips", {"track_id": "nope"})[0], 404)


if __name__ == "__main__":
    unittest.main()
