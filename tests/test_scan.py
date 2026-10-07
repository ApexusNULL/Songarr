"""Music already in the music folder (from anywhere) is found and used instead of downloaded again."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from mutagen.id3 import ID3, TIT2, TPE1, TSRC

from songarr import tagging
from songarr.scan import artist_fits, from_name, read_tags
from songarr.service import Service

from tests.helpers import FakeYouTube, audio_file, spotify_track

ONE_SECOND = 1000  # the test tones are one second long


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.music = self.dir / "Music"
        self.music.mkdir()
        self.youtube = FakeYouTube()
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.youtube)
        self.svc.db.set_setting("library_root", str(self.music))

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, rel: str, ext: str = "mp3") -> Path:
        path = self.music / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio_file(ext), path)
        return path

    def id3(self, path: Path, title=None, artist=None, isrc=None) -> None:
        tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if isrc:
            tags.add(TSRC(encoding=3, text=isrc))
        tags.save(path)

    def status(self, n: int) -> tuple[str, str | None]:
        r = self.svc.db.one("SELECT status, file_path FROM tracks WHERE id = ?", (f"{n:022d}",))
        return r["status"], r["file_path"]

    def test_finds_songs_by_songarrs_tag_isrc_or_title_and_artist(self):
        made_before = self.put("Old Songarr/Song 1.m4a", "m4a")  # a file Songarr made on another PC
        tagging.tag(made_before, spotify_track(1), None)
        ripped = self.put("Rips/track07.mp3")  # someone else's file: different title, same recording
        self.id3(ripped, title="Song Two (Remastered 2011)", artist="Somebody", isrc="USABC1234567")
        named = self.put("Artist 0/Album 1 (2020)/03 - Song 3.mp3")  # no tags at all: read from the names
        tagged = self.put("misc/x.mp3")
        self.id3(tagged, title="Song 4 (feat. Guest)", artist="Artist 1, Guest")
        wrong = self.put("misc/y.mp3")
        self.id3(wrong, title="Song 5", artist="Another Band")  # same title, different artist: not it
        self.svc.db.upsert_tracks([
            spotify_track(1, duration_ms=ONE_SECOND),
            spotify_track(2, isrc="USABC1234567"),
            spotify_track(3, duration_ms=ONE_SECOND),
            spotify_track(4, duration_ms=ONE_SECOND),
            spotify_track(5, duration_ms=ONE_SECOND),
            spotify_track(6, title="Song 6", duration_ms=200_000),
        ])
        state = self.svc.scanner.scan()
        self.assertEqual((state["files"], state["found"], state["error"]), (5, 4, None))
        self.assertEqual(self.status(1), ("downloaded", str(made_before)))
        self.assertEqual(self.status(2), ("downloaded", str(ripped)))
        self.assertEqual(self.status(3), ("downloaded", str(named)))
        self.assertEqual(self.status(4), ("downloaded", str(tagged)))
        self.assertEqual(self.status(5)[0], "wanted")
        self.assertEqual(self.status(6)[0], "wanted")
        self.assertTrue(named.exists())  # used where it is, never moved
        self.assertIn("Found 4 songs already in your music folder",
                      self.svc.db.one("SELECT message FROM history WHERE event = 'imported'")[0])
        self.assertTrue(self.svc.scanner.ready.is_set())

    def test_another_songs_file_is_copied_into_this_songs_album(self):
        original = self.put("Artist 1/Album 1 (2020)/01 - Song 1.m4a", "m4a")
        tagging.tag(original, spotify_track(1), None)
        hits = spotify_track(11, title="Song 1", artists=["Artist 1"], album="Greatest Hits", album_artists=["Artist 1"],
                             track_number=7, duration_ms=ONE_SECOND)
        self.svc.db.upsert_tracks([spotify_track(1, duration_ms=ONE_SECOND), hits])
        self.svc.db.set_status(f"{1:022d}", "downloaded", file_path=str(original), file_size=original.stat().st_size,
                               youtube_id="yt-original")
        self.assertEqual(self.svc.scanner.scan()["found"], 1)
        status, path = self.status(11)
        self.assertEqual((status, Path(path)), ("downloaded", self.music / "Artist 1" / "Greatest Hits (2020)" / "07 - Song 1.m4a"))
        self.assertEqual(read_tags(Path(path))["spotify_id"], f"{11:022d}")  # tagged for this song
        self.assertEqual(self.svc.db.one("SELECT youtube_id FROM tracks WHERE id = ?", (f"{11:022d}",))[0], "yt-original")
        self.assertEqual(self.status(1), ("downloaded", str(original)))  # the other album keeps its own

    def test_folders_named_with_dots_are_looked_in(self):
        self.put("Artist 2/...And More (2021)/01 - Song 2.mp3")
        self.put("$RECYCLE.BIN/S-1-5/01 - Old.mp3")  # but not the recycle bin
        self.assertEqual(self.svc.scanner.scan()["files"], 1)

    def test_a_length_that_doesnt_fit_is_another_recording(self):
        self.id3(self.put("a.mp3"), title="Song 7", artist="Artist 1")  # 1 second; the song is 3 minutes
        self.svc.db.upsert_tracks([spotify_track(7)])
        self.assertEqual(self.svc.scanner.scan()["found"], 0)

    def test_later_scans_only_read_new_files(self):
        self.put("Artist 1/Album/01 - Song 1.mp3")
        b = self.put("Artist 2/Album/01 - Song 2.mp3")
        self.assertEqual(self.svc.scanner.scan()["checked"], 2)
        self.assertEqual(self.svc.scanner.scan()["checked"], 0)
        b.unlink()
        self.put("Artist 3/Album/01 - Song 3.mp3")
        state = self.svc.scanner.scan()
        self.assertEqual((state["checked"], self.svc.scanner.info()["indexed"]), (1, 2))  # the gone file left the index

    def test_a_song_added_after_the_scan_is_found_before_downloading(self):
        path = self.put("Artist 2/Hits/05 - Song 8.mp3")
        self.svc.scanner.scan()
        self.svc.db.upsert_tracks([spotify_track(8, duration_ms=ONE_SECOND)])
        t = dict(self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (f"{8:022d}",)))
        t["artists"] = ["Artist 2"]
        self.svc.process(t)
        self.assertEqual(self.status(8), ("downloaded", str(path)))
        self.assertEqual(self.youtube.max_active, 0)  # YouTube was never asked

    def test_an_unreachable_folder_doesnt_hold_downloads_up(self):
        self.svc.db.set_setting("library_root", str(self.dir / "not here"))
        state = self.svc.scanner.scan()
        self.assertIn("can't be reached", state["error"])
        self.assertTrue(self.svc.scanner.ready.is_set())


class TagTests(unittest.TestCase):
    def test_reading(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            m4a = tmp / "a.m4a"
            shutil.copyfile(audio_file("m4a"), m4a)
            tagging.tag(m4a, spotify_track(9, isrc="usabc9999999"), None)
            tags = read_tags(m4a)
            self.assertEqual((tags["title"], tags["artist"], tags["spotify_id"]), ("Song 9", "Artist 0", f"{9:022d}"))
            self.assertEqual(tags["isrc"], "USABC9999999")
            self.assertAlmostEqual(tags["duration"], 1.0, delta=0.2)
            opus = tmp / "b.opus"
            shutil.copyfile(audio_file("opus"), opus)
            tagging.tag(opus, spotify_track(10), None)
            self.assertEqual(read_tags(opus)["title"], "Song 10")
            self.assertEqual(read_tags(tmp / "missing.mp3")["title"], "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_names_and_artists(self):
        self.assertEqual(from_name(Path("Queen/A Night at the Opera (1975)/11 - Bohemian Rhapsody.flac")),
                         {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"})
        self.assertEqual(from_name(Path("X/Y/1-02. Song.mp3"))["title"], "Song")
        self.assertTrue(artist_fits(["Simon & Garfunkel"], "Simon & Garfunkel"))
        self.assertTrue(artist_fits(["Drake", "Future"], "Drake feat. Future"))
        self.assertTrue(artist_fits(["Daft Punk"], "Daft Punk, Pharrell Williams"))
        self.assertFalse(artist_fits(["Simon"], "Paul Simon"))
        self.assertFalse(artist_fits([], "Anyone"))


if __name__ == "__main__":
    unittest.main()
