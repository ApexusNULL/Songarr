"""Jams (listening together) and push notifications, offline."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from songarr import jams as jams_mod
from songarr import push as push_mod
from songarr.appapi import AppAPI, make_app_server
from songarr.jams import JamError, Jams
from songarr.service import Service

from tests.helpers import FakeYouTube, audio_file, spotify_track
from tests.test_platform import free_port


class JamLogicTests(unittest.TestCase):
    def setUp(self):
        self.jams = Jams()
        self.songs = [{"id": f"s{i}", "title": f"Song {i}", "duration_ms": 180000} for i in range(3)]

    def test_shared_timeline(self):
        jam = self.jams.start(1, self.songs, 0, 0, True, invite=[2])
        self.assertGreater(jam.ref, time.time())  # starts a moment ahead so every phone is ready
        self.assertEqual(jam.position_now(jam.ref + 2.5), 2500)
        self.assertEqual(jam.position_now(jam.ref - 1), 0)
        with self.assertRaises(JamError):
            self.jams.control(jam.id, 2, "pause", {})  # only members control it
        self.jams.join(jam.id, 2)
        self.jams.control(jam.id, 2, "pause", {"position_ms": 4000})
        self.assertEqual((jam.playing, jam.position_now(time.time() + 10)), (False, 4000))
        self.jams.control(jam.id, 1, "seek", {"position_ms": 60000})
        self.assertEqual(jam.position_ms, 60000)
        v = jam.version
        self.jams.control(jam.id, 1, "next", {"expected_index": 0})
        self.jams.control(jam.id, 2, "next", {"expected_index": 0})  # both phones hit the end together: one skip
        self.assertEqual((jam.index, jam.version), (1, v + 1))
        self.jams.control(jam.id, 1, "add", {}, [{"id": "s9"}])
        self.assertEqual([s["id"] for s in jam.queue], ["s0", "s1", "s2", "s9"])
        self.jams.control(jam.id, 2, "add", {"next": True}, [{"id": "s8"}])  # "play next": right after what's on
        self.assertEqual([s["id"] for s in jam.queue], ["s0", "s1", "s8", "s2", "s9"])
        self.jams.control(jam.id, 1, "replace", {"index": 1}, [{"id": "a"}, {"id": "b"}])
        self.assertEqual((jam.index, jam.queue[jam.index]["id"], jam.playing), (1, "b", True))

    def test_leaving_hands_over_and_ends(self):
        jam = self.jams.start(1, self.songs, 0, 0, True, invite=[2, 3])
        self.jams.join(jam.id, 2)
        self.jams.decline(jam.id, 3)
        self.assertEqual(self.jams.current(3), (None, []))
        self.jams.leave(jam.id, 1)
        self.assertEqual(jam.host, 2)  # still going for Sam
        self.jams.leave(jam.id, 2)
        self.assertTrue(jam.ended)
        with self.assertRaises(JamError):
            self.jams.get(jam.id, 2)

    def test_one_jam_at_a_time(self):
        a = self.jams.start(1, self.songs, 0, 0, True, invite=[2])
        b = self.jams.start(3, self.songs, 0, 0, True, invite=[2])
        self.jams.join(a.id, 2)
        self.jams.join(b.id, 2)
        self.assertNotIn(2, a.members)
        self.assertEqual(self.jams.current(2)[0], b)

    def test_quiet_members_drop_out(self):
        jam = self.jams.start(1, self.songs, 0, 0, True, invite=[])
        jam.members[1] = time.time() - jams_mod.MEMBER_TIMEOUT - 1
        self.assertEqual(self.jams.current(1), (None, []))

    def test_waiting_phones_hear_changes_at_once(self):
        jam = self.jams.start(1, self.songs, 0, 0, True, invite=[])
        v = jam.version
        threading.Timer(0.2, lambda: self.jams.control(jam.id, 1, "pause", {})).start()
        t = time.time()
        self.assertEqual(self.jams.wait(jam.id, 1, v, timeout=5).version, v + 1)
        self.assertLess(time.time() - t, 2)
        t = time.time()
        self.assertEqual(self.jams.wait(jam.id, 1, v + 1, timeout=0.3).version, v + 1)  # nothing new: times out
        self.assertGreaterEqual(time.time() - t, 0.25)


class _FakeGoogle(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        g = self.server
        if self.path == "/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            header, claims, sig = form["assertion"].split(".")
            g.jwt_ok = g.verify(f"{header}.{claims}".encode(), base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
            g.claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
            return self._json(200, {"access_token": "at-1", "expires_in": 3600})
        msg = json.loads(body)["message"]
        g.sent.append((self.headers["Authorization"], msg))
        if msg["token"] == "gone":
            return self._json(404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}})
        return self._json(200, {"name": "projects/p/messages/1"})

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class JamApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.svc = Service(cls.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        db = cls.svc.db
        db.upsert_tracks([spotify_track(1), spotify_track(2)])
        f = cls.dir / "song1.m4a"
        shutil.copy(audio_file("m4a"), f)
        db.set_status(f"{1:022d}", "downloaded", file_path=str(f), file_size=f.stat().st_size)
        cls.svc.podcasts.upsert([{"id": "ap1", "title": "History Hour", "artwork_url": "https://img/show.jpg"}])
        db.run("""INSERT INTO podcast_episodes(id, podcast_id, guid, title, published, duration_ms, audio_url)
                  VALUES ('ep0123456789abcdef', 'ap1', 'g1', 'Rome', 0, 600000, 'https://x.example/rome.mp3')""")
        cls.svc.users.rename(1, "Alex")
        cls.sam = cls.svc.users.create("Sam")
        cls.port = free_port()
        cls.http = make_app_server(cls.svc, "127.0.0.1", cls.port)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.tok = {}
        for uid in (1, cls.sam):
            code, _ = cls.svc.users.new_pairing_code(uid)
            cls.tok[uid] = cls.call(None, "POST", "/api/v1/auth/pair", {"code": code, "device": "Phone"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def call(cls, token, method, path, body=None):
        h = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"null")

    def test_jam_with_sam(self):
        me, sam = self.tok[1], self.tok[self.sam]
        self.assertEqual(self.call(me, "GET", "/api/v1/people")[1], {"people": [{"id": self.sam, "name": "Sam"}]})
        st, jam = self.call(me, "POST", "/api/v1/jams", {
            "items": [{"track_id": f"{1:022d}"}, {"track_id": f"{2:022d}"}, {"episode_id": "ep0123456789abcdef"}],
            "index": 0, "position_ms": 0, "playing": True, "invite": [self.sam]})
        self.assertEqual(st, 200)
        self.assertEqual([i["id"] for i in jam["queue"]], [f"{1:022d}", "ep0123456789abcdef"])  # song 2 isn't downloaded
        self.assertEqual(jam["queue"][1]["kind"], "episode")
        self.assertEqual([m["name"] for m in jam["invited"]], ["Sam"])
        self.assertGreater(jam["ref"], jam["server_time"])

        cur = self.call(sam, "GET", "/api/v1/jams/current")[1]
        self.assertIsNone(cur["jam"])
        self.assertEqual((cur["invites"][0]["id"], cur["invites"][0]["from"]), (jam["id"], "Alex"))
        self.assertEqual(self.call(sam, "POST", f"/api/v1/jams/{jam['id']}/control", {"action": "pause"})[0], 403)
        joined = self.call(sam, "POST", f"/api/v1/jams/{jam['id']}/join")[1]
        self.assertEqual(sorted(m["name"] for m in joined["members"]), ["Alex", "Sam"])

        # Sam pauses; my phone, waiting for news, hears it straight away
        got = {}
        waiter = threading.Thread(target=lambda: got.update(self.call(me, "GET", f"/api/v1/jams/{jam['id']}?v={joined['version']}")[1]))
        waiter.start()
        time.sleep(0.3)
        self.call(sam, "POST", f"/api/v1/jams/{jam['id']}/control", {"action": "pause", "position_ms": 3000})
        waiter.join(10)
        self.assertEqual((got["playing"], got["position_ms"]), (False, 3000))

        self.call(me, "POST", f"/api/v1/jams/{jam['id']}/control", {"action": "add", "items": [{"episode_id": "ep0123456789abcdef"}]})
        self.assertEqual(len(self.call(sam, "GET", f"/api/v1/jams/{jam['id']}")[1]["queue"]), 3)
        self.call(me, "POST", f"/api/v1/jams/{jam['id']}/leave")
        left = self.call(sam, "GET", f"/api/v1/jams/{jam['id']}")[1]
        self.assertEqual((left["host"]["name"], [m["name"] for m in left["members"]]), ("Sam", ["Sam"]))
        self.call(sam, "POST", f"/api/v1/jams/{jam['id']}/leave")
        self.assertEqual(self.call(sam, "GET", f"/api/v1/jams/{jam['id']}")[0], 404)
        self.assertEqual(self.call(me, "POST", "/api/v1/jams", {"items": [{"track_id": f"{2:022d}"}]})[0], 400)  # nothing playable

    def test_invites_are_pushed_to_phones(self):
        from Cryptodome.Hash import SHA256
        from Cryptodome.PublicKey import RSA
        from Cryptodome.Signature import pkcs1_15
        key = RSA.generate(2048)
        google = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGoogle)
        google.sent, google.jwt_ok = [], False

        def verify(msg, sig):
            try:
                pkcs1_15.new(key.publickey()).verify(SHA256.new(msg), sig)
                return True
            except ValueError:
                return False
        google.verify = verify
        threading.Thread(target=google.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{google.server_address[1]}"
        account = {"project_id": "songarr-test", "client_email": "push@songarr-test.iam.gserviceaccount.com",
                   "private_key": key.export_key().decode(), "token_uri": f"{base}/token"}
        (self.svc.data_dir / "firebase-service-account.json").write_text(json.dumps(account), encoding="utf-8")
        orig = push_mod.FCM
        push_mod.FCM = base
        try:
            st, res = self.call(self.tok[self.sam], "POST", "/api/v1/devices/push", {"token": "sam-phone"})
            self.assertEqual((st, res), (200, {"ok": True, "push": True}))
            self.svc.db.run("INSERT INTO devices(user_id, name, token_hash, created, push_token) VALUES (?, 'Old', 'x', 0, 'gone')",
                            (self.sam,))
            sent = self.svc.push.send_to_users([self.sam], "Alex started a Jam", "Listen together: Song 1", {"jam": "abc"})
            self.assertEqual(sent, 1)
            self.assertTrue(google.jwt_ok)  # signed with the service account's key
            self.assertEqual(google.claims["scope"], push_mod.SCOPE)
            auth, msg = next((a, m) for a, m in google.sent if m["token"] == "sam-phone")
            self.assertEqual((auth, msg["notification"]["title"], msg["data"]), ("Bearer at-1", "Alex started a Jam", {"jam": "abc"}))
            self.assertIsNone(self.svc.db.one("SELECT push_token FROM devices WHERE name = 'Old'")["push_token"])  # uninstalled app forgotten
        finally:
            push_mod.FCM = orig
            (self.svc.data_dir / "firebase-service-account.json").unlink()
            google.shutdown()
            google.server_close()


if __name__ == "__main__":
    unittest.main()


class JamTopUpTests(unittest.TestCase):
    """When the queue runs out, the server adds songs the people in the Jam like."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        db = self.svc.db
        db.upsert_tracks([spotify_track(i) for i in range(1, 8)])
        for i in range(1, 7):  # song 7 isn't downloaded
            db.set_status(f"{i:022d}", "downloaded", file_path=str(self.dir / f"{i}.m4a"), file_size=1)
        self.sam = self.svc.users.create("Sam")
        for uid, songs in ((1, (2, 3, 7)), (self.sam, (3, 4))):
            for i in songs:
                db.run("INSERT INTO likes(user_id, track_id, created) VALUES (?, ?, 0)", (uid, f"{i:022d}"))
        self.api = AppAPI(self.svc)
        self.svc.jams.more = self.api.jam_more
        self.items = self.api._jam_items(1, [{"track_id": f"{1:022d}"}])

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_keeps_going_with_shared_likes_first(self):
        jams = self.svc.jams
        jam = jams.start(1, self.items, 0, 0, True, invite=[self.sam])
        jams.join(jam.id, self.sam)
        jams.control(jam.id, 1, "next", {"expected_index": 0})  # the one song ended
        ids = [t["id"] for t in jam.queue]
        self.assertEqual((jam.index, jam.playing), (1, True))
        self.assertEqual(ids[1], f"{3:022d}")  # both like song 3
        self.assertEqual(set(ids[2:]), {f"{2:022d}", f"{4:022d}"})  # then anyone's likes that are on the server
        self.assertTrue(jam.queue[1]["jam_added"])

    def test_a_playlist_goes_round_again(self):
        jams = self.svc.jams
        playlist = self.api._jam_items(1, [{"track_id": f"{i:022d}"} for i in range(1, 7)])  # songs 1-6
        jam = jams.start(1, playlist, 4, 0, True, [])
        for expected in (4, 5):
            jams.control(jam.id, 1, "next", {"expected_index": expected})
        self.assertEqual((jam.index, jam.queue[jam.index]["id"], jam.playing), (6, f"{1:022d}", True))  # back to song 1
        self.assertEqual(len(jam.queue), 12)
        # someone picks from another list: the Jam carries on from that one
        jams.control(jam.id, 1, "replace", {"index": 0}, playlist[2:4])
        self.assertEqual([t["id"] for t in jam.source], [f"{3:022d}", f"{4:022d}"])

    def test_long_lists_are_windowed(self):
        long = [{"id": f"t{i}"} for i in range(1200)]
        jam = self.svc.jams.start(1, long, 900, 0, True, [])
        self.assertEqual(jam.queue[jam.index]["id"], "t900")
        self.assertLessEqual(len(jam.queue), jams_mod.MAX_QUEUE)

    def test_podcasts_just_end(self):
        jams = self.svc.jams
        jam = jams.start(1, [{"id": "ep1", "kind": "episode", "title": "Rome"}], 0, 0, True, [])
        jams.control(jam.id, 1, "next", {"expected_index": 0})
        self.assertEqual((len(jam.queue), jam.playing), (1, False))
