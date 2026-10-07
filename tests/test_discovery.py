"""Made-for-you recommendations and podcasts (all offline: Deezer, Apple and RSS feeds are faked)."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from songarr import discover, podcasts
from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube, audio_file, spotify_track
from tests.test_platform import free_port

AUDIO = bytes(range(256)) * 40  # a pretend episode

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>History Hour</title>
  <link>https://history.example</link>
  <description>&lt;p&gt;Stories from the past.&lt;/p&gt;</description>
  <itunes:author>Pod Co</itunes:author>
  <itunes:image href="https://img.example/show.jpg"/>
  <item>
    <title>Older episode</title>
    <guid>guid-1</guid>
    <pubDate>Mon, 01 Jun 2026 10:00:00 +0000</pubDate>
    <itunes:duration>05:00</itunes:duration>
    <enclosure url="{base}/redirect" type="audio/mpeg" length="10240"/>
    <description>Plain notes</description>
  </item>
  <item>
    <title>Newest episode</title>
    <guid>guid-2</guid>
    <pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate>
    <itunes:duration>1:02:03</itunes:duration>
    <enclosure url="{base}/ep.mp3" type="audio/mpeg" length="10240"/>
    <content:encoded><![CDATA[<p>Hello <b>world</b></p><p>Second &amp; last</p>]]></content:encoded>
  </item>
  <item>
    <title>A blog post, not an episode</title>
    <guid>guid-3</guid>
  </item>
</channel>
</rss>"""


class _FeedHandler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.path == "/feed.xml":
            body = FEED.replace("{base}", base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"{base}/ep.mp3")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/ep.mp3":
            start, end, status = 0, len(AUDIO) - 1, 200
            if rng := self.headers.get("Range"):
                a, b = rng.removeprefix("bytes=").split("-")
                start, end, status = int(a), int(b) if b else len(AUDIO) - 1, 206
            self.send_response(status)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(AUDIO)}")
            self.end_headers()
            self.wfile.write(AUDIO[start: end + 1])
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def song(n: int, title: str, artist: str, **kw) -> dict:
    return spotify_track(n, title=title, artists=[artist], album_artists=[artist], **kw)


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.feeds = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
        threading.Thread(target=cls.feeds.serve_forever, daemon=True).start()
        cls.feed_base = f"http://127.0.0.1:{cls.feeds.server_address[1]}"
        podcasts.allow_private_hosts = True  # the fake feed lives on this PC

        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        cls.sam = cls.svc.users.create("Sam")
        cls.kid = cls.svc.users.create("Kid")
        mine = [song(1, "Sugar, We're Goin Down", "Northbound"), song(2, "Dance, Dance", "Northbound"),
                                song(3, "Thnks fr th Mmrs", "Northbound"), song(4, "Misery Business", "Glasswing")]
        theirs = [song(5, "Satellites", "Northbound")]
        db = cls.svc.db
        db.upsert_tracks(mine + theirs)
        with db.tx() as c:  # what each person likes (the last one most recently)
            for uid, songs in ((1, mine), (cls.sam, theirs)):
                c.executemany("INSERT INTO likes(user_id, track_id, created) VALUES (?, ?, ?)",
                              [(uid, t["id"], float(n)) for n, t in enumerate(songs)])
                c.executemany("INSERT INTO track_sources(track_id, source) VALUES (?, ?)", [(t["id"], f"like:{uid}") for t in songs])
        db.recompute_monitored()
        cls.svc.podcasts.upsert([{"id": "ap123", "title": "History Hour", "author": "Pod Co", "artwork_url": "https://img.example/a.jpg",
                                  "feed_url": f"{cls.feed_base}/feed.xml", "genre": "History", "genre_ids": ["1487"]}])
        cls.svc.podcasts.follow(1, "ap123")
        # Sam's "Satellites" is downloaded: on the server, but Alex hasn't saved it
        f = cls.dir / "satellites.m4a"
        shutil.copy(audio_file("m4a"), f)
        cls.svc.db.set_status(f"{5:022d}", "downloaded", file_path=str(f), file_size=f.stat().st_size)

        cls.port = free_port()
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.tokens = {}
        for uid in (1, cls.kid):
            code, _ = cls.svc.users.new_pairing_code(uid)
            cls.tokens[uid] = cls.call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        podcasts.allow_private_hosts = False
        for s in (cls.http, cls.feeds):
            s.shutdown()
            s.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def call(cls, token, method, path, body=None, headers=None, raw=False):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                payload = r.read()
                return r.status, payload if raw else (json.loads(payload) if payload else None), r.headers
        except urllib.error.HTTPError as e:
            with e:
                payload = e.read()
                return e.code, payload if raw else (json.loads(payload) if payload else None), e.headers

    def get(self, path, uid=1):
        return self.call(self.tokens[uid], "GET", path)

    # -- fake Deezer -----------------------------------------------------------------

    def fake_deezer(self):
        artists = {"Northbound": 404, "Glasswing": 1000}
        info = {1: "The Lanterns", 2: "Velvet Static", 3: "June Hart", 404: "Northbound", 1000: "Glasswing"}

        def artist(i):
            return {"deezer_id": i, "name": info[i], "image_url": f"https://img/{i}.jpg", "thumb_url": f"https://img/{i}s.jpg",
                    "fans": 10}

        def track(i, title, who):
            return {"deezer_id": i, "title": title, "artists": [who], "album": "A", "duration_ms": 200000, "explicit": False,
                    "cover_url": f"https://c/{i}", "thumb_url": f"https://t/{i}", "rank": 1}

        related = {404: [artist(1000), artist(1), artist(2)], 1000: [artist(3), artist(1)]}
        tops = {1: [track(11, "Paper Skies, Count Me In", "The Lanterns")], 2: [track(12, "Open Road", "Velvet Static")],
                3: [track(13, "Simmer", "June Hart")], 1000: [track(14, "Misery Business", "Glasswing")],
                404: [track(15, "Sugar, We're Goin Down", "Northbound"), track(16, "Satellites", "Northbound"),
                      track(17, "My Songs Know What You Did in the Dark", "Northbound")]}
        patches = {
            (discover, "find_artist"): lambda name: artist(artists[name]) if name in artists else None,
            (discover, "related_artists"): lambda i, limit=12: related.get(i, []),
            (discover, "artist_top"): lambda i, limit=10: tops.get(i, [])[:limit],
            (discover, "chart_artists"): lambda limit=15: [artist(2)],
            (discover, "genre_chart"): lambda gid, limit=50: [track(12, "Open Road", "Velvet Static")],
            (podcasts, "top"): lambda genre_id=None, limit=25: (
                [{"id": "ap555", "title": "Ancient Worlds", "author": "A", "artwork_url": None, "feed_url": None,
                  "genre": "History", "genre_ids": ["1487"]}] if genre_id == "1487" else
                [{"id": "ap1", "title": "The Daily", "author": "NYT", "artwork_url": None, "feed_url": None, "genre": "News",
                  "genre_ids": ["1489"]}, {"id": "ap123", "title": "History Hour", "author": "Pod Co", "artwork_url": None,
                                           "feed_url": None, "genre": "History", "genre_ids": ["1487"]}]),
        }
        saved = {k: getattr(*k) for k in patches}
        for (mod, name), fn in patches.items():
            setattr(mod, name, fn)
        self.addCleanup(lambda: [setattr(mod, name, fn) for (mod, name), fn in saved.items()])

    # -- recommendations --------------------------------------------------------------

    def test_recommendations_follow_taste(self):
        self.fake_deezer()
        data = self.svc.recommender.refresh(1)
        self.assertEqual(data["top_artists"][0]["name"], "Northbound")
        names = [a["name"] for a in data["artists"]]
        self.assertIn("The Lanterns", names)
        self.assertNotIn("Glasswing", names)  # already a favourite
        self.assertNotIn("Northbound", names)
        alt = next(a for a in data["artists"] if a["name"] == "The Lanterns")
        self.assertEqual(alt["reason"], "Because you like Northbound")

        home = self.get("/api/v1/home")[1]
        songs = home["for_you"]["songs"]
        titles = [s["title"] for s in songs]
        self.assertIn("Paper Skies, Count Me In", titles)
        self.assertNotIn("Sugar, We're Goin Down", titles)  # liked already
        self.assertNotIn("Misery Business", titles)
        satellites = next(s for s in songs if s["title"] == "Satellites")
        self.assertEqual(satellites["track"]["id"], f"{5:022d}")  # on the server: playable now
        self.assertTrue(satellites["track"]["playable"])
        dear = next(s for s in songs if s["title"].startswith("Paper Skies"))
        self.assertFalse((dear["track"] or {}).get("playable"))  # needs adding (and downloading) first
        self.assertEqual(dear["source"], "deezer")
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(home["liked_count"], 4)
        # podcasts: the History genre of a followed show, minus what's already followed
        recs = [p["id"] for p in home["for_you"]["podcasts"]]
        self.assertIn("ap555", recs)
        self.assertNotIn("ap123", recs)

    def test_new_profile_gets_popular_picks(self):
        self.fake_deezer()
        data = self.svc.recommender.refresh(self.kid)
        self.assertEqual([a["reason"] for a in data["artists"]], ["Popular right now"])
        self.assertEqual([s["title"] for s in data["songs"]], ["Open Road"])
        self.assertEqual({p["reason"] for p in data["podcasts"]}, {"Popular right now"})

    def test_home_works_before_recommendations_exist(self):
        self.svc.db.run("DELETE FROM recommendations WHERE user_id = ?", (self.kid,))
        home = self.get("/api/v1/home", self.kid)[1]
        self.assertIsNone(home["for_you"])
        self.assertIn(self.kid, self.svc.recommender._pending)  # being prepared in the background

    def test_artist_page_has_popular_songs_and_similar_artists(self):
        self.fake_deezer()
        res = self.get("/api/v1/artist?name=Northbound")[1]
        self.assertEqual(res["about"]["deezer_id"], 404)
        pop = {p["title"]: p["in_library"] for p in res["popular"]}
        self.assertEqual(pop["Satellites"], f"{5:022d}")
        self.assertIsNone(pop["My Songs Know What You Did in the Dark"])
        self.assertIn("The Lanterns", [a["name"] for a in res["related"]])

    def test_preview_then_like_to_download(self):
        orig_preview, orig_full = discover.preview_url, discover.full_track
        asked = []
        discover.preview_url = lambda deezer_id=None, isrc=None, artist="", title="": (
            asked.append((deezer_id, isrc, artist, title)) or (f"{self.feed_base}/ep.mp3" if deezer_id == 11 or isrc else None))
        discover.full_track = lambda i: {
            "id": f"dz{i}", "title": "Paper Skies, Count Me In", "artists": ["The Lanterns"], "album": "Paper Skies EP", "album_artists": [],
            "album_id": None, "release_date": None, "track_number": 1, "disc_number": 1, "duration_ms": 182000, "isrc": None,
            "explicit": False, "cover_url": None, "thumb_url": None}
        try:
            st, body, h = self.call(self.tokens[1], "GET", "/api/v1/preview?source=deezer&id=11", raw=True)
            self.assertEqual((st, body, h["Content-Type"]), (200, AUDIO, "audio/mpeg"))
            st, body, _ = self.call(self.tokens[1], "GET", "/api/v1/preview?source=spotify&id=x&isrc=USX000000002&artist=A&title=T",
                                    headers={"Range": "bytes=0-3"}, raw=True)
            self.assertEqual((st, body), (206, AUDIO[:4]))
            self.assertEqual(asked[-1], (None, "USX000000002", "A", "T"))
            self.assertEqual(self.call(self.tokens[1], "GET", "/api/v1/preview?source=deezer&id=12")[0], 404)
            self.assertEqual(self.call(None, "GET", "/api/v1/preview?source=deezer&id=11")[0], 401)
            # Like on the preview: downloads it and saves it to Liked Songs
            st, track, _ = self.call(self.tokens[1], "POST", "/api/v1/requests", {"source": "deezer", "id": 11, "like": True})
            self.assertEqual((st, track["id"], track["liked"], track["status"]), (200, "dz11", True, "wanted"))
            self.assertIn("dz11", [t["id"] for t in self.get("/api/v1/library/liked")[1]["tracks"]])
            self.call(self.tokens[1], "DELETE", "/api/v1/likes/dz11")  # leave the taste profile as other tests expect
        finally:
            discover.preview_url, discover.full_track = orig_preview, orig_full

    # -- podcasts ----------------------------------------------------------------------

    def test_followed_podcasts(self):
        overview = self.get("/api/v1/podcasts")[1]
        self.assertEqual([p["id"] for p in overview["following"]], ["ap123"])
        self.assertEqual(self.get("/api/v1/podcasts", self.kid)[1]["following"], [])

    def test_episodes_stream_and_resume(self):
        st, show, _ = self.get("/api/v1/podcasts/ap123")
        self.assertEqual(st, 200)
        self.assertTrue(show["following"] and not show["on_spotify"])
        self.assertEqual(show["description"], "Stories from the past.")
        eps = show["episodes"]
        self.assertEqual([e["title"] for e in eps], ["Newest episode", "Older episode"])  # no-audio item skipped
        self.assertEqual(eps[0]["duration_ms"], 3723000)
        self.assertEqual(eps[1]["duration_ms"], 300000)
        self.assertEqual(eps[0]["description"], "Hello world\n\nSecond & last")

        st, body, h = self.call(self.tokens[1], "GET", f"/api/v1/podcasts/episodes/{eps[0]['id']}/stream",
                                headers={"Range": "bytes=2-9"}, raw=True)
        self.assertEqual((st, body, h["Content-Range"]), (206, AUDIO[2:10], f"bytes 2-9/{len(AUDIO)}"))
        st, body, _ = self.call(self.tokens[1], "GET", f"/api/v1/podcasts/episodes/{eps[1]['id']}/stream", raw=True)
        self.assertEqual((st, body), (200, AUDIO))  # through the feed's redirect
        self.assertEqual(self.call(self.tokens[1], "GET", "/api/v1/podcasts/episodes/ep0000000000000000/stream")[0], 404)
        self.assertEqual(self.call(None, "GET", f"/api/v1/podcasts/episodes/{eps[0]['id']}/stream")[0], 401)

        self.call(self.tokens[1], "POST", f"/api/v1/podcasts/episodes/{eps[0]['id']}/progress",
                  {"position_ms": 60000, "duration_ms": 3723000, "completed": False})
        home = self.get("/api/v1/home")[1]
        self.assertEqual([(e["id"], e["progress_ms"]) for e in home["podcasts"]["continue"]], [(eps[0]["id"], 60000)])
        again = self.get("/api/v1/podcasts/ap123")[1]["episodes"][0]
        self.assertEqual(again["progress_ms"], 60000)

    def test_following_in_the_app(self):
        self.svc.podcasts.upsert([{"id": "ap777", "title": "Tech Talk", "feed_url": f"{self.feed_base}/feed.xml"}])
        self.assertEqual(self.call(self.tokens[self.kid], "PUT", "/api/v1/podcasts/ap777/follow")[1], {"following": True})
        self.assertIn("ap777", [p["id"] for p in self.get("/api/v1/podcasts", self.kid)[1]["following"]])
        self.assertEqual(self.call(self.tokens[self.kid], "DELETE", "/api/v1/podcasts/ap777/follow")[1],
                         {"following": False, "on_spotify": False})
        # one brought over from Spotify is followed like any other
        self.assertEqual(self.call(self.tokens[1], "DELETE", "/api/v1/podcasts/ap123/follow")[1],
                         {"following": False, "on_spotify": False})
        self.assertEqual(self.call(self.tokens[1], "PUT", "/api/v1/podcasts/ap123/follow")[1], {"following": True})

    def test_downloads_expire_unlike_songs(self):
        dl = self.svc.podcast_downloads
        self.svc.db.set_setting("podcast_root", str(self.dir / "Podcasts"))
        self.svc.podcasts.upsert([{"id": "ap321", "title": "Daily Drop", "feed_url": f"{self.feed_base}/feed.xml"}])
        self.svc.podcasts.refresh("ap321", 0)
        newest, older = [r["id"] for r in self.svc.db.q(
            "SELECT id FROM podcast_episodes WHERE podcast_id = 'ap321' ORDER BY published DESC")]
        self.assertEqual(self.call(self.tokens[1], "PUT", "/api/v1/podcasts/ap321/downloads", {"keep": "forever"})[0], 400)
        st, body, _ = self.call(self.tokens[1], "PUT", "/api/v1/podcasts/ap321/downloads", {"keep": "played"})
        self.assertEqual((st, body), (200, {"downloads": "played"}))
        dl.cycle()
        # only recent episodes are fetched automatically (the older one is months old)
        kept = self.get("/api/v1/podcasts/downloads")[1]["episodes"]
        self.assertEqual([(e["id"], e["keep"], e["on_server"]) for e in kept], [(newest, "played", True)])
        path = Path(self.svc.db.one("SELECT path FROM episode_files WHERE episode_id = ?", (newest,))["path"])
        self.assertEqual(path.read_bytes(), AUDIO)
        self.assertEqual(path.parent.name, "Daily Drop")
        st, body, h = self.call(self.tokens[1], "GET", f"/api/v1/podcasts/episodes/{newest}/stream", headers={"Range": "bytes=0-9"}, raw=True)
        self.assertEqual((st, body), (206, AUDIO[:10]))
        self.assertIn("ETag", h)  # served from the server's copy
        self.assertEqual(self.get("/api/v1/podcasts/ap321")[1]["downloads"], "played")

        # the kid keeps the same episode for a week; another one by hand
        self.call(self.tokens[self.kid], "PUT", f"/api/v1/podcasts/episodes/{newest}/download", {"keep": "7"})
        st, info, _ = self.call(self.tokens[self.kid], "PUT", f"/api/v1/podcasts/episodes/{older}/download", {"keep": "3"})
        self.assertEqual((st, info["keep"]), (200, "3"))
        self.assertEqual(self.call(self.tokens[self.kid], "PUT", f"/api/v1/podcasts/episodes/{older}/download", {"keep": "off"})[0], 400)
        dl.cycle()
        self.assertTrue(self.get("/api/v1/podcasts/downloads", self.kid)[1]["episodes"][1]["on_server"])

        # finishing an "until played" episode releases it, but the file stays while the kid keeps it
        self.call(self.tokens[1], "POST", f"/api/v1/podcasts/episodes/{newest}/progress", {"position_ms": 3723000, "completed": True})
        dl.cycle()
        self.assertEqual(self.get("/api/v1/podcasts/downloads")[1]["episodes"], [])
        self.assertTrue(path.exists())
        # ...and it isn't fetched again for the finished listener
        self.assertIsNotNone(self.svc.db.one("SELECT released FROM episode_holds WHERE user_id = 1 AND episode_id = ?", (newest,))["released"])

        # a week later the kid's copies run out and the files are deleted
        self.svc.db.run("UPDATE episode_holds SET created = created - 8 * 86400 WHERE user_id = ?", (self.kid,))
        dl.cycle()
        self.assertEqual(self.get("/api/v1/podcasts/downloads", self.kid)[1]["episodes"], [])
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())  # the empty show folder goes too
        self.assertIsNone(self.svc.db.one("SELECT 1 FROM episode_files"))

        # turning the show off drops what it downloaded automatically, not episodes kept by hand
        self.call(self.tokens[self.kid], "PUT", "/api/v1/podcasts/ap321/downloads", {"keep": "14"})
        self.svc.db.run("DELETE FROM episode_holds WHERE user_id = ?", (self.kid,))
        dl.cycle()
        self.call(self.tokens[self.kid], "PUT", f"/api/v1/podcasts/episodes/{older}/download", {"keep": "30"})
        self.call(self.tokens[self.kid], "PUT", "/api/v1/podcasts/ap321/downloads", {"keep": "off"})
        dl.cycle()
        self.assertEqual([e["id"] for e in self.get("/api/v1/podcasts/downloads", self.kid)[1]["episodes"]], [older])
        self.call(self.tokens[self.kid], "DELETE", f"/api/v1/podcasts/episodes/{older}/download")
        dl.cycle()
        self.assertIsNone(self.svc.db.one("SELECT 1 FROM episode_files"))

    # -- artist bios and lyrics --------------------------------------------------------------

    def test_history_chapters_from_an_article(self):
        from songarr import artist_info
        article = "\n".join([
            "Band X is an American rock band formed in 2001. They are loud.", "",
            "== Early life ==", "The founders met at school in 1999 and played in garages.", "",
            "== History ==",
            "=== 2001–2004: Formation ===", "Band X formed in 2001. " + "They toured a lot. " * 40, "",
            "=== 2005–present: Fame ===", "Their second album went platinum in 2005.", "",
            "== Musical style ==", "Loud and fast.", "",
            "== 2010–2012: A side project ==", "They made a jazz record.", "",
            "== Discography ==", "Album one", ""])
        chapters = artist_info.history(article)
        self.assertEqual([c["heading"] for c in chapters],
                         ["Early life", "2001–2004: Formation", "2005–present: Fame", "2010–2012: A side project"])
        self.assertLessEqual(len(chapters[1]["text"]), artist_info.CHAPTER_CHARS)
        self.assertTrue(chapters[1]["text"].endswith("."))  # cut at a sentence
        self.assertEqual(artist_info.sections(article)[0][2].split(".")[0], "Band X is an American rock band formed in 2001")

    def test_the_right_wikipedia_page_is_chosen(self):
        from songarr import artist_info
        pages = {"query": {"pages": [
            {"index": 1, "title": "Queen (disambiguation)", "description": "", "pageprops": {"disambiguation": ""}},
            {"index": 2, "title": "Queen Elizabeth", "description": "British monarch"},
            {"index": 3, "title": "Queen (band)", "description": "British rock band"},
        ]}}
        orig = artist_info._api
        artist_info._api = lambda params: pages
        try:
            self.assertEqual(artist_info.find_page("Queen")["title"], "Queen (band)")
            pages["query"]["pages"] = [{"index": 1, "title": "Violin", "description": "Bowed string instrument"}]
            self.assertIsNone(artist_info.find_page("violin"))  # not a musician
        finally:
            artist_info._api = orig

    def test_artist_bio_is_fetched_once_and_cached(self):
        from songarr import artist_info
        calls = []
        orig = artist_info.fetch
        artist_info.fetch = lambda name: calls.append(name) or (
            {"name": name, "title": "Northbound", "summary": "An American rock band.", "history": [], "facts": {"formed": "2001"}}
            if name == "Northbound" else None)
        worker = threading.Thread(target=self.svc.artist_info.run, daemon=True)
        worker.start()
        try:
            st, res, _ = self.get("/api/v1/artist/about?name=Northbound")
            self.assertEqual((st, res["pending"], res["about"]["facts"]), (200, False, {"formed": "2001"}))
            self.get("/api/v1/artist/about?name=northbound")  # same artist, any spelling: from the cache
            self.assertEqual(calls, ["Northbound"])
            self.assertEqual(self.get("/api/v1/artist/about?name=Nobody%20Famous")[1], {"about": None, "pending": False})
            self.get("/api/v1/artist/about?name=Nobody%20Famous")
            self.assertEqual(calls, ["Northbound", "Nobody Famous"])  # misses are cached too
            self.assertEqual(self.get("/api/v1/artist/about?name=")[0], 400)
        finally:
            artist_info.fetch = orig

    def test_lastfm_fills_in_when_wikipedia_has_no_page(self):
        import io
        import urllib.request
        from songarr import artist_info
        reply = {"artist": {"name": "Scene Queen", "url": "https://www.last.fm/music/Scene+Queen",
                            "tags": {"tag": [{"name": "bimbocore"}, {"name": "metalcore"}]},
                            "bio": {"summary": 'Scene Queen is a singer. <a href="https://www.last.fm/music/Scene+Queen">Read more on Last.fm</a>',
                                    "content": "Scene Queen is a singer.\n" + "She toured the world in 2023 and released several records. " * 3
                                               + "\nUser-contributed text is available under the Creative Commons By-SA License."}}}

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig_find, orig_open = artist_info.find_page, urllib.request.urlopen
        artist_info.find_page = lambda name: None  # no Wikipedia article
        urllib.request.urlopen = lambda req, timeout=None: Reply(json.dumps(reply).encode())
        artist_info.client.lastfm_key = "0" * 32
        try:
            got = artist_info.fetch("Scene Queen")
            self.assertEqual((got["source"], got["summary"], got["facts"]), ("Last.fm", "Scene Queen is a singer.", {"genres": ["bimbocore", "metalcore"]}))
            self.assertEqual(len(got["history"]), 1)
            self.assertNotIn("Creative Commons", got["history"][0]["text"])
            artist_info.client.lastfm_key = ""
            self.assertIsNone(artist_info.fetch("Scene Queen"))  # no key: Wikipedia only
        finally:
            artist_info.find_page, urllib.request.urlopen = orig_find, orig_open
            artist_info.client.lastfm_key = ""

    def test_search_puts_the_named_artist_first(self):
        self.fake_deezer()
        res = self.get("/api/v1/search?q=Glasswing")[1]
        self.assertEqual(res["top_artist"]["name"], "Glasswing")
        self.assertEqual(res["top_artist"]["image_url"], "https://img/1000.jpg")
        self.assertIsNone(self.get("/api/v1/search?q=misery")[1]["top_artist"])  # a song title, not an artist

    def test_lyrics(self):
        from songarr import lyrics
        self.assertEqual(lyrics.parse_lrc("[00:01.50] One\n[01:02.25][00:30.00] Two\nno stamp\n"),
                         [{"t": 1500, "text": "One"}, {"t": 30000, "text": "Two"}, {"t": 62250, "text": "Two"}])
        asked = []

        def fake(path, **q):
            asked.append(path)
            if path == "get":
                return None  # not found by exact album: search instead
            return [{"trackName": "Dance, Dance", "artistName": "Northbound", "duration": 500, "syncedLyrics": "[00:01.00] wrong"},
                    {"trackName": "Dance, Dance", "artistName": "Northbound", "duration": 181,
                     "syncedLyrics": "[00:02.00] la la\n[00:04.00] la", "plainLyrics": "la la\nla"}]
        orig = lyrics._get
        lyrics._get = fake
        try:
            st, res, _ = self.get(f"/api/v1/lyrics/{2:022d}")  # "Dance, Dance", 180 s
            self.assertEqual(st, 200)
            self.assertEqual([x["t"] for x in res["synced"]], [2000, 4000])  # the one within 3 s of the song's length
            self.assertEqual((res["plain"], res["source"]), ("la la\nla", "LRCLIB"))
            n = len(asked)
            self.get(f"/api/v1/lyrics/{2:022d}")
            self.assertEqual(len(asked), n)  # cached
            lyrics._get = lambda path, **q: None if path == "get" else []
            self.assertEqual(self.get(f"/api/v1/lyrics/{3:022d}")[0], 404)
            self.assertEqual(self.get("/api/v1/lyrics/nosuchsong")[0], 404)
        finally:
            lyrics._get = orig

    def test_outside_addresses_only(self):
        podcasts.allow_private_hosts = False
        try:
            for url in ("http://127.0.0.1/feed", "http://10.1.2.3/x", "http://192.168.1.1/", "http://[::1]/", "http://169.254.169.254/",
                        "file:///etc/passwd", "ftp://example.com/x"):
                with self.assertRaises(podcasts.UnsafeURL, msg=url):
                    podcasts.check_url(url)
            podcasts.check_url("https://93.184.216.34/feed")  # a public address is fine
            req = urllib.request.Request("https://93.184.216.34/ep.mp3")
            with self.assertRaises(podcasts.UnsafeURL):  # and so are redirects into the home network
                podcasts._CheckedRedirects().redirect_request(req, None, 302, "Found", {}, "http://192.168.0.10/admin")
        finally:
            podcasts.allow_private_hosts = True

    def test_feed_parsing_edge_cases(self):
        self.assertEqual(podcasts.parse_duration("90"), 90000)
        self.assertEqual(podcasts.parse_duration("12:34"), 754000)
        self.assertIsNone(podcasts.parse_duration("soon"))
        with self.assertRaises(podcasts.PodcastError):
            podcasts.parse_feed(b"<html>not a feed", "ap1")
        show, eps = podcasts.parse_feed(FEED.replace("{base}", "https://h.example").encode(), "ap1")
        self.assertEqual(show["author"], "Pod Co")
        self.assertEqual(len({e["id"] for e in eps}), 2)
        _, again = podcasts.parse_feed(FEED.replace("{base}", "https://h.example").encode(), "ap1")
        self.assertEqual([e["id"] for e in eps], [e["id"] for e in again])  # ids are stable across reads


if __name__ == "__main__":
    unittest.main()
