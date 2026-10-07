from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from songarr import library, tagging
from songarr.matching import Candidate, TrackInfo, score, strip_title, title_similarity
from songarr.web import youtube_id

from tests.helpers import FFMPEG, audio_file, spotify_track

T = TrackInfo("Lose Yourself", ["Eminem"], "Curtain Call: The Hits", 326.0)


def cand(title, duration=326, channel="Eminem - Topic", artists=("Eminem",), track=None, source="ytm", album=None):
    return score(T, Candidate(id="x", title=title, duration=duration, channel=channel, artists=list(artists),
                              track=track, source=source, album=album))


class MatchingTests(unittest.TestCase):
    def test_official_audio_wins(self):
        self.assertGreaterEqual(cand("Lose Yourself").score, 0.95)

    def test_same_artist_same_length_wrong_song_is_not_auto_accepted(self):
        self.assertLess(cand("Square Dance", duration=324).score, 0.7)

    def test_cover_by_someone_else_is_not_auto_accepted(self):
        c = cand("8 Mile - Lose Yourself - Main Theme", channel="Geek Music", artists=("Geek Music",))
        self.assertLess(c.score, 0.7)

    def test_length_is_decisive(self):
        self.assertEqual(cand("Lose Yourself", duration=400).score, 0.0)  # >30 s off: different edit
        self.assertGreater(cand("Lose Yourself", duration=327).score, cand("Lose Yourself", duration=338).score)

    def test_version_words_must_agree(self):
        for bad in ["Lose Yourself (Live)", "Lose Yourself (Remix)", "Lose Yourself sped up", "Lose Yourself (Instrumental)",
                    "Lose Yourself - Karaoke Version", "Lose Yourself [1 HOUR]"]:
            self.assertLess(cand(bad).score, 0.7, bad)
        live = TrackInfo("Lose Yourself - Live", ["Eminem"], None, 326)
        self.assertGreaterEqual(score(live, Candidate("x", "Lose Yourself (Live)", 326, "Eminem - Topic", ["Eminem"], source="ytm")).score, 0.9)
        self.assertLess(score(live, Candidate("x", "Lose Yourself", 326, "Eminem - Topic", ["Eminem"], source="ytm")).score, 0.7)

    def test_live_album_context_does_not_punish_plain_titles(self):
        t = TrackInfo("Hotel California", ["Eagles"], "Hell Freezes Over (Live)", 432)
        c = score(t, Candidate("x", "Hotel California", 433, "Eagles - Topic", ["Eagles"], album="Hell Freezes Over (Live)", source="ytm"))
        self.assertGreaterEqual(c.score, 0.9)

    def test_music_video_with_artist_in_title(self):
        c = cand("Eminem - Lose Yourself [HD]", duration=330, channel="EminemVEVO", artists=(), source="yt")
        self.assertGreaterEqual(c.score, 0.7)

    def test_artist_spacing_does_not_matter(self):
        t = TrackInfo("キラーボール - 2022 Remaster", ["Gesu No Kiwami Otome"], "ベストアルバム「丸」", 289)
        c = score(t, Candidate("x", "キラーボール - Killer Ball", 290, "gesunokiwami otome", ["gesunokiwami otome"], source="ytm"))
        self.assertGreaterEqual(c.score, 0.7, c.reasons)
        t = TrackInfo("Song", ["PornoGraffitti"], None, 200)
        self.assertGreaterEqual(score(t, Candidate("x", "Song", 200, "Porno Graffitti", ["Porno Graffitti"], source="ytm")).score, 0.7)

    def test_isrc_with_exact_length_bridges_scripts(self):
        # Real cases from the library: same recording, title or artist in another script.
        t = TrackInfo("Joendanyusho", ["Creepy Nuts"], "INDIES COMPLETE", 240, "JPU901800001")
        c = score(t, Candidate("x", "助演男優賞", 240, "Creepy Nuts", ["Creepy Nuts"], track="助演男優賞", source="isrc"))
        self.assertGreaterEqual(c.score, 0.7, c.reasons)
        t = TrackInfo("お先に失礼します。", ["花冷え。"], "来世は偉人！", 191, "JPX000000001")
        c = score(t, Candidate("x", "お先に失礼します。 - Pardon Me, I Have To Go Now.", 191, "HANABIE.", ["HANABIE."], source="isrc"))
        self.assertGreaterEqual(c.score, 0.7, c.reasons)
        # without the ISRC evidence (or with a different length) it still waits for review
        c = score(t, Candidate("x", "お先に失礼します - Punching Out", 194, "tegacreampan", ["tegacreampan"], source="ytm"))
        self.assertLess(c.score, 0.7)
        t2 = TrackInfo("Joendanyusho", ["Creepy Nuts"], None, 240)
        self.assertLess(score(t2, Candidate("x", "合法的トビ方ノススメ", 238, "Creepy Nuts", ["Creepy Nuts"], source="ytm")).score, 0.7)
        self.assertLess(score(t2, Candidate("x", "助演男優賞", 236, "Creepy Nuts", ["Creepy Nuts"], source="isrc")).score, 0.7)

    def test_title_cleanup(self):
        self.assertEqual(strip_title("Song (feat. X) - 2011 Remaster"), "Song")
        self.assertEqual(strip_title("Uptown Funk (feat. Bruno Mars)"), "Uptown Funk")
        self.assertEqual(strip_title("Dance with Me"), "Dance with Me")
        self.assertEqual(strip_title("Hey Jude - Remastered 2015"), "Hey Jude")
        self.assertEqual(title_similarity("Beyoncé", "Beyonce"), 1.0)

    def test_youtube_link_parsing(self):
        for text in ["dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1", "https://youtu.be/dQw4w9WgXcQ",
                     "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD", " https://youtube.com/shorts/dQw4w9WgXcQ "]:
            self.assertEqual(youtube_id(text), "dQw4w9WgXcQ", text)
        self.assertIsNone(youtube_id("not a link"))


class LibraryTests(unittest.TestCase):
    def test_layout(self):
        p = library.relative_path(spotify_track(3, title="Intro: Part 1", album="Live/Loud", release_date="1999-03-01"), "m4a")
        self.assertEqual(p, Path("Artist 0") / "Live-Loud (1999)" / "03 - Intro - Part 1.m4a")
        p = library.relative_path(spotify_track(7, disc_number=2), "m4a")
        self.assertEqual(p.name, "2-07 - Song 7.m4a")

    def test_windows_unsafe_names(self):
        self.assertEqual(library.safe('What? "Yes" <No> | *'), "What 'Yes' No -")
        self.assertEqual(library.safe("CON"), "_CON")
        self.assertEqual(library.safe("aux.txt"), "_aux.txt")
        self.assertEqual(library.safe("Trailing dots..."), "Trailing dots")
        self.assertEqual(library.safe(""), "Unknown")
        self.assertLessEqual(len(library.safe("x" * 400)), 120)

    def test_place_moves_atomically(self):
        d = Path(tempfile.mkdtemp())
        try:
            src = d / "a.m4a"
            src.write_bytes(b"data")
            dest = library.place(src, d / "lib" / "Artist" / "Album" / "01 - x.m4a")
            self.assertEqual(dest.read_bytes(), b"data")
            self.assertFalse(src.exists())
            self.assertEqual([p.name for p in dest.parent.iterdir()], ["01 - x.m4a"])
        finally:
            shutil.rmtree(d)


@unittest.skipUnless(FFMPEG, "FFmpeg not installed")
class TaggingTests(unittest.TestCase):
    def test_roundtrip_all_formats(self):
        from mutagen import File

        t = spotify_track(5, title="Title ✓", artists=["A", "B"], album_artists=["A"], isrc="USABC2600001", explicit=True)
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 64
        d = Path(tempfile.mkdtemp())
        try:
            for ext in ("m4a", "mp3", "opus"):
                p = d / f"x.{ext}"
                shutil.copy(audio_file(ext), p)
                tagging.tag(p, t, cover, "dQw4w9WgXcQ")
                self.assertEqual(tagging.read_spotify_id(p), t["id"], ext)
                f = File(p, easy=True)
                self.assertEqual(f["title"][0], "Title ✓", ext)
                self.assertEqual(f["artist"][0], "A, B", ext)
                self.assertEqual(f["album"][0], "Album 1", ext)
                self.assertEqual(f["tracknumber"][0].split("/")[0], "5", ext)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
