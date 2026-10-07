"""Following artists, and hearing about their new releases (in the app and on phones)."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube

TODAY = date.today()


def day(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def release(album_id: int, title: str, released: str, kind: str = "album") -> dict:
    return {"source": "deezer", "id": album_id, "name": title, "artists": ["The Band"], "type": kind, "count": None,
            "explicit": False, "cover_url": f"https://img/{album_id}-xl.jpg", "thumb_url": f"https://img/{album_id}.jpg",
            "release_date": released}


class ReleasesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.r = self.svc.releases
        self.r.in_background = False
        self.catalogue = {7: [release(1, "Old Album", "2001-05-01"), release(2, "Last Year", day(400)),
                              release(3, "Recent Single", day(20), "single")]}
        self.r.fetch = lambda deezer_id, name: list(self.catalogue.get(deezer_id, []))
        self.r.find = lambda name: {"deezer_id": 7, "name": "The Band", "thumb_url": "https://img/band.jpg"} if "band" in name.lower() else None
        self.pushed = []
        self.svc.push.send_async = lambda *args, **kw: self.pushed.append((args, kw))
        self.alex = 1
        self.sam = self.svc.users.create("Sam")

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_following_doesnt_announce_whats_already_out(self):
        self.r.follow(self.alex, "the band")
        self.assertEqual([a["name"] for a in self.r.following(self.alex)], ["The Band"])
        self.assertEqual(self.r.following(self.alex)[0]["latest"]["name"], "Recent Single")
        self.assertEqual(self.r.notifications(self.alex)["unread"], 0)
        self.assertEqual(self.pushed, [])
        self.assertEqual([a["name"] for a in self.r.recent(self.alex)], ["Recent Single"])  # Home: out in the last 60 days
        with self.assertRaises(LookupError):
            self.r.follow(self.alex, "Nobody At All")

    def test_a_new_release_reaches_every_follower(self):
        self.r.follow(self.alex, "The Band")
        self.r.follow(self.sam, "The Band", 7)
        self.catalogue[7] += [release(4, "Brand New", day(0), "single"), release(5, "Remaster From 1999", "1999-01-01")]
        self.assertEqual(self.r.check(), 1)  # the old remaster just appeared on Deezer: not news
        for uid in (self.alex, self.sam):
            n = self.r.notifications(uid)
            self.assertEqual(n["unread"], 1)
            self.assertEqual((n["notifications"][0]["title"], n["notifications"][0]["body"]), ("New single from The Band", "Brand New"))
            self.assertEqual(n["notifications"][0]["album"]["id"], 4)  # what tapping it opens
        (users, title, body, data), kw = self.pushed[0]
        self.assertEqual((sorted(users), title, data["type"], data["album"], kw["channel"]),
                         ([self.alex, self.sam], "New single from The Band", "new_release", "4", "releases"))
        self.assertEqual(self.r.check(), 0)  # only once
        self.r.mark_read(self.alex)
        self.assertEqual((self.r.unread(self.alex), self.r.unread(self.sam)), (0, 1))
        self.assertIn("Brand New", self.svc.db.one("SELECT message FROM history WHERE event = 'release'")[0])

    def test_following_from_the_artist_page_keeps_their_photo(self):
        self.r.follow(self.alex, "The Band", 7)  # the app sends the name and Deezer id, not the photo
        self.assertEqual(self.r.following(self.alex)[0]["image_url"], "https://img/band.jpg")

    def test_unfollowing(self):
        self.r.follow(self.alex, "The Band")
        self.r.follow(self.sam, "The Band")
        self.r.unfollow(self.sam, 7)
        self.catalogue[7].append(release(6, "Another One", day(1)))
        self.r.check()
        self.assertEqual((self.r.unread(self.alex), self.r.unread(self.sam)), (1, 0))
        self.r.unfollow(self.alex, 7)
        self.r.check()  # nobody follows them now: forgotten
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM artist_releases")[0], 0)

    def test_removing_a_profile_removes_their_follows(self):
        self.r.follow(self.sam, "The Band")
        self.catalogue[7].append(release(8, "Fresh", day(0)))
        self.r.check()
        self.svc.users.delete(self.sam)
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM artist_follows")[0], 0)
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM notifications")[0], 0)


class ReleasesAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        r = cls.svc.releases
        r.in_background = False
        cls.catalogue = [release(31, "Debut", day(700)), release(32, "Single Out Now", day(3), "single")]
        r.fetch = lambda deezer_id, name: list(cls.catalogue)
        r.find = lambda name: {"deezer_id": 99, "name": "The Band"} if name.lower() == "the band" else None
        cls.svc.push.send_async = lambda *a, **k: None
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        code, _ = cls.svc.users.new_pairing_code(1)
        cls.token = cls.call("POST", "/api/v1/auth/pair", {"code": code, "device": "Test phone"}, token=None)[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def call(cls, method, path, body=None, token=""):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token or cls.token}"
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_follow_from_the_artist_page_to_the_bell(self):
        self.svc.releases.find = lambda name: {"deezer_id": 99, "name": "The Band"} if name.lower() == "the band" else None
        status, followed = self.call("POST", "/api/v1/artists/follow", {"name": "The Band", "deezer_id": 99})
        self.assertEqual((status, followed["following"]), (200, True))
        self.assertEqual(self.call("POST", "/api/v1/artists/follow", {"name": "Nobody Known"})[0], 404)
        artists = self.call("GET", "/api/v1/artists/following")[1]["artists"]
        self.assertEqual([(a["name"], a["latest"]["name"]) for a in artists], [("The Band", "Single Out Now")])
        releases = self.call("GET", "/api/v1/releases")[1]["releases"]
        self.assertEqual([(a["name"], a["type"], a["on_server"]) for a in releases], [("Single Out Now", "single", 0)])
        home = self.call("GET", "/api/v1/home")[1]
        self.assertEqual(([a["name"] for a in home["releases"]], home["unread_notifications"]), (["Single Out Now"], 0))

        self.catalogue.append(release(33, "Surprise Album", day(0)))
        self.assertEqual(self.svc.releases.check(), 1)
        n = self.call("GET", "/api/v1/notifications")[1]
        self.assertEqual((n["unread"], n["notifications"][0]["title"]), (1, "New album from The Band"))
        self.assertEqual(n["notifications"][0]["album"]["name"], "Surprise Album")
        self.assertEqual(self.call("POST", "/api/v1/notifications/read", {})[1]["unread"], 0)

        self.assertEqual(self.call("POST", "/api/v1/artists/unfollow", {"deezer_id": 99})[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/artists/following")[1]["artists"], [])
        self.assertEqual(self.call("POST", "/api/v1/artists/unfollow", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
