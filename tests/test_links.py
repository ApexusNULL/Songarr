"""Songs from links (POST /requests/link): a YouTube video, or anything else yt-dlp can download,
pasted or shared into the app; that very upload downloads, into your library or onto a playlist."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr import links, podcasts
from songarr.appapi import make_app_server
from songarr.db import track_dict
from songarr.service import Service

from tests.helpers import FakeYouTube, spotify_track
from tests.test_platform import free_port

VIDEO = "https://www.youtube.com/watch?v=abcDEF12345"
MUSIC = "https://music.youtube.com/watch?v=xyz_-987654"
DEMO = "https://soundcloud.com/someone/a-demo"


class LinkTrackTests(unittest.TestCase):
    def test_what_the_song_is_called(self):
        t = links.link_track(FakeYouTube.LINKS[VIDEO], VIDEO)
        self.assertEqual((t["id"], t["title"], t["artists"], t["release_date"], t["duration_ms"], t["cover_url"]),
                         ("yt" + b"abcDEF12345".hex(), "Night Drive", ["Nova Band"], "2024-03-01", 215_000,
                          "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg"))
        t = links.link_track(FakeYouTube.LINKS[MUSIC], MUSIC)  # YouTube Music's own details
        self.assertEqual((t["title"], t["artists"], t["album"]), ("Morning Light", ["Nova Band", "Guest"], "First Light"))
        self.assertTrue(t["id"].isalnum())  # (a YouTube id can have - and _)
        t = links.link_track(FakeYouTube.LINKS[DEMO], DEMO)
        self.assertEqual((t["title"], t["artists"], t["thumb_url"], t["duration_ms"]), ("A Demo", ["someone"], "https://img/big.jpg", 95_500))
        self.assertTrue(t["id"].startswith("ln"))
        for title, want in [("Song (Lyrics)", "Song"), ("Song [Official Lyric Video]", "Song"), ("Song (Live)", "Song (Live)"),
                            ("Song (Official Audio)", "Song")]:
            self.assertEqual(links.link_track({"title": title, "channel": "Band - Topic"}, "x")["title"], want)
        self.assertEqual(links.link_track({"title": "Song", "channel": "Band - Topic"}, "x")["artists"], ["Band"])


class RequestLinkTests(unittest.TestCase):
    def setUp(self):
        podcasts.allow_private_hosts = True  # (no DNS in tests: the home-network check is tried below on its own)
        self.dir = Path(tempfile.mkdtemp())
        self.yt = FakeYouTube(delay=0.01)
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: self.yt)  # (not started: songs download by hand)
        self.svc.db.set_setting("library_root", str(self.dir / "Music"))
        self.port = free_port()
        self.http = make_app_server(self.svc, "127.0.0.1", self.port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        code, _ = self.svc.users.new_pairing_code(1)
        self.token = self.call("POST", "/auth/pair", {"code": code, "device": "Phone"}, token=None)[1]["token"]
        self.pid = self.call("POST", "/playlists", {"name": "Road trip"})[1]["id"]

    def tearDown(self):
        podcasts.allow_private_hosts = False
        self.http.shutdown()
        self.http.server_close()
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, method, path, body=None, token=""):
        token = self.token if token == "" else token
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/v1{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def download(self, tid: str) -> dict:
        self.svc.process(track_dict(self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))))
        return dict(self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (tid,)))

    def playlist(self) -> list[str]:
        return [t["title"] for t in self.call("GET", f"/playlists/{self.pid}")[1]["tracks"]]

    def test_a_youtube_link_onto_a_playlist(self):
        st, t = self.call("POST", "/requests/link", {"url": f"Listen to this! {VIDEO} :)", "playlist_id": self.pid})
        self.assertEqual((st, t["title"], t["artists"], t["status"], t["playable"]), (200, "Night Drive", ["Nova Band"], "wanted", False))
        self.assertEqual(self.playlist(), ["Night Drive"])
        self.assertEqual([r["title"] for r in self.call("GET", "/requests")[1]["requests"]], ["Night Drive"])
        row = self.svc.db.one("SELECT * FROM tracks WHERE id = ?", (t["id"],))
        self.assertEqual((row["youtube_id"], row["pinned"], row["monitored"], row["priority"]), ("abcDEF12345", 1, 1, 10))
        row = self.download(t["id"])
        self.assertEqual((row["status"], row["youtube_id"]), ("downloaded", "abcDEF12345"))
        self.assertEqual(self.yt.downloads, ["abcDEF12345"])  # that video: no searching
        self.assertTrue(Path(row["file_path"]).is_file())

        st, again = self.call("POST", "/requests/link", {"url": VIDEO})  # the same link again: the same song
        self.assertEqual((st, again["id"], again["playable"]), (200, t["id"], True))
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM tracks")[0], 1)

    def test_a_link_to_another_site(self):
        st, t = self.call("POST", "/requests/link", {"url": DEMO})
        self.assertEqual((st, t["title"], t["artists"]), (200, "A Demo", ["someone"]))
        self.assertEqual(self.playlist(), [])  # (no playlist asked for: just your library)
        row = self.download(t["id"])
        self.assertEqual((row["status"], row["youtube_id"], row["source_url"]), ("downloaded", None, DEMO))
        self.assertEqual(self.yt.downloads, [DEMO])

    def test_a_video_already_downloaded_for_another_song(self):
        self.svc.db.upsert_tracks([spotify_track(1, title="Night Drive", artists=["Nova Band"])])
        self.svc.db.set_status(f"{1:022d}", "downloaded", youtube_id="abcDEF12345", file_path=str(self.dir / "1.m4a"))
        st, t = self.call("POST", "/requests/link", {"url": VIDEO, "playlist_id": self.pid})
        self.assertEqual((st, t["id"], t["playable"]), (200, f"{1:022d}", True))
        self.assertEqual(self.playlist(), ["Night Drive"])

    def test_links_that_dont_work(self):
        def error(body):
            st, d = self.call("POST", "/requests/link", body)
            return st, d["error"]

        self.assertEqual(error({"url": "just some words"})[0], 400)
        st, msg = error({"url": "https://www.youtube.com/playlist?list=PL123"})
        self.assertEqual((st, msg), (400, "That link can't be downloaded: That's a playlist or a channel; share a link to one song."))
        self.assertEqual(error({"url": "https://example.com/not-a-song"})[0], 400)
        podcasts.allow_private_hosts = False
        st, msg = error({"url": "http://10.1.2.3/admin"})
        self.assertEqual((st, msg), (400, "Songarr only downloads from the internet, not from addresses in your own network."))
        podcasts.allow_private_hosts = True
        self.svc.users.create("Sam")
        other = self.svc.db.one("SELECT id FROM users WHERE name = 'Sam'")[0]
        self.svc.db.run("UPDATE user_playlists SET user_id = ? WHERE id = ?", (other, self.pid))
        self.assertEqual(error({"url": VIDEO, "playlist_id": self.pid})[0], 403)  # someone else's: nothing added
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) FROM tracks")[0], 0)
        self.svc.cooldown_until = time.time() + 600
        self.assertEqual(error({"url": VIDEO})[0], 503)
        self.assertEqual(self.call("POST", "/requests/link", {"url": VIDEO}, token=None)[0], 401)


if __name__ == "__main__":
    unittest.main()
