"""Each song's genre (Deezer's, for its album) and era (when the recording first came out, from
MusicBrainz), looked up in the background so lists can be sorted and grouped by them."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from songarr import discover, genres
from songarr.appapi import AppAPI
from songarr.service import Service

from tests.helpers import FakeYouTube, spotify_track

DEEZER = {
    "/track/isrc:USRAP0000001": {"id": 1, "album": {"id": 10}},
    "/track/isrc:GBQUE0000002": {"id": 2, "album": {"id": 20}},
    "/track/77": {"id": 77, "album": {"id": 30}},
    "/album/10": {"genre_id": 116, "genres": {"data": [{"id": 116, "name": "Rap/Hip Hop"}]}},
    "/album/20": {"genre_id": 152, "genres": {"data": [{"id": 152, "name": "Rock"}, {"id": 132, "name": "Pop"}]}},
    "/album/30": {"genre_id": 113, "genres": {"data": []}},  # (no list: its genre by id)
    "/album/40": {"genre_id": 132, "genres": {"data": [{"id": 132, "name": "Pop"}]}},
    "/genre/113": {"id": 113, "name": "Dance"},
}
MUSICBRAINZ = {
    "/isrc/USRAP0000001?fmt=json": {"recordings": [{"first-release-date": "1988-05-03"}, {"first-release-date": "1991"}]},
    "/isrc/GBQUE0000002?fmt=json": {"recordings": [{"first-release-date": "1975-10-31"}]},  # (the remaster's code: the 1975 recording)
}


def fake_deezer(path: str, ttl: float) -> dict:
    if path.startswith("/search/track?q=") and "Paper%20Planes" in path:  # (a cover first: not it)
        return {"data": [{"id": 4, "title": "Paper Planes", "artist": {"name": "Someone Else"}, "album": {"id": 10}},
                         {"id": 5, "title": "Paper Planes", "artist": {"name": "M.I.A."}, "album": {"id": 40}}]}
    if path.startswith("/search/track"):
        return {"data": []}
    if path in DEEZER:
        return DEEZER[path]
    raise RuntimeError("Deezer: no data")


class GenreTests(unittest.TestCase):
    def setUp(self):
        self.orig = discover._get
        discover._get = fake_deezer
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.g = self.svc.genres
        self.asked: list[str] = []

        def mb(path):
            self.asked.append(path)
            if path.startswith("/recording?query=") and "Paper%20Planes" in path:
                return {"recordings": [{"score": 100, "first-release-date": "2007-08-20"}, {"score": 60, "first-release-date": "1990"}]}
            return MUSICBRAINZ.get(path)

        self.g._mb = mb

    def tearDown(self):
        discover._get = self.orig
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def facts(self, tid: str) -> tuple:
        r = self.svc.db.one("SELECT genre, year, facts_checked IS NOT NULL FROM tracks WHERE id = ?", (tid,))
        return r[0], r[1], bool(r[2])

    def test_genre_and_the_year_it_first_came_out(self):
        db = self.svc.db
        db.upsert_tracks([
            spotify_track(1, title="Rhymes", artists=["MC Nova"], isrc="USRAP0000001", release_date="1988-05-03"),
            spotify_track(2, title="Bohemian Rhapsody - Remastered 2011", artists=["Queen"], isrc="GBQUE0000002", release_date="2011-01-01"),
            spotify_track(3, title="Paper Planes", artists=["M.I.A."], release_date="2008-02-11"),  # no recording code: searched
            spotify_track(4, title="Deep Cut", artists=["MC Nova"], release_date="1990"),  # Deezer has nothing: the artist's genre
            spotify_track(5, title="Rhymes", artists=["MC Nova"], album="Greatest Hits", release_date="2005"),  # the same song, later
            {**spotify_track(6, title="Floor Filler", artists=["DJ X"], release_date="2019-01-01"), "id": "dz77"},
        ])
        self.assertEqual(self.g.check(), 6)
        self.assertEqual(self.facts(f"{1:022d}"), ("Rap", 1988, True))  # the first of its two dates
        self.assertEqual(self.facts(f"{2:022d}"), ("Rock", 1975, True))  # the album's first genre; the recording's year, not the remaster's
        self.assertEqual(self.facts(f"{3:022d}"), ("Pop", 2007, True))  # (the close match, not the 60% one)
        self.assertEqual(self.facts(f"{4:022d}"), ("Rap", 1990, True))
        self.assertEqual(self.facts(f"{5:022d}"), ("Rap", 1988, True))  # the earliest of the same song in the library
        self.assertEqual(self.facts("dz77"), ("Dance", 2019, True))
        self.assertEqual(self.g.check(), 0)  # once each
        out = AppAPI.track_out(db.one("SELECT * FROM tracks WHERE id = ?", (f"{2:022d}",)), set())
        self.assertEqual((out["genre"], out["year"]), ("Rock", 1975))

    def test_without_the_internet_nothing_is_marked(self):
        def offline(path, ttl):
            raise OSError("no network")

        discover._get = offline
        self.svc.db.upsert_tracks([spotify_track(1, isrc="USRAP0000001")])
        with self.assertRaises(genres.Offline):
            self.g.check()
        self.assertEqual(self.facts(f"{1:022d}"), (None, None, False))  # (tried again later)


if __name__ == "__main__":
    unittest.main()
