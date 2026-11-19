"""How the songs someone added are getting on: the progress the app shows (GET /requests/progress) and
the notification that follows them through their stages to "ready"."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube, spotify_track
from tests.test_platform import free_port


def tid(n: int) -> str:
    return f"{n:022d}"


class DownloadsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())  # (not started: songs move by hand)
        self.svc.scanner.ready.set()
        self.d = self.svc.downloads
        self.clock = 1_000_000.0
        self.d.now = lambda: self.clock
        self.sent: list[dict] = []
        self.svc.push.send_data = lambda uids, data, urgent=False, collapse=None, ttl=3600: self.sent.append(
            data | {"to": uids, "urgent": urgent, "ttl": ttl}) or 1

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, *ns: int, user: int = 1, **kw) -> None:
        for n in ns:
            self.svc.request_track(user, spotify_track(n, title=kw.get("title", f"Song {n}"), artists=["Nova Band"]), log=False)

    def status(self, n: int, status: str, **fields) -> None:
        if status == "downloaded":
            fields.setdefault("file_path", str(self.dir / f"{n}.m4a"))
        self.svc.db.set_status(tid(n), status, **fields)

    def working(self, n: int, stage: str, percent: float = 0.0) -> None:
        self.svc.active[tid(n)] = {"id": tid(n), "title": f"Song {n}", "stage": stage, "percent": percent}

    def done(self, n: int) -> None:
        self.svc.active.pop(tid(n), None)
        self.status(n, "downloaded")

    def test_progress_counts_the_batch_and_what_each_song_is_doing(self):
        self.assertIsNone(self.d.progress(1))  # nothing asked for
        self.svc.db.upsert_tracks([spotify_track(9)])
        self.status(9, "downloaded")
        time.sleep(0.01)
        self.request(9)  # on the server already: it joins their library, nothing to download
        self.request(1, 2, 3, 4, 5)
        self.assertEqual(self.d.progress(1)["total"], 5)
        self.status(1, "downloaded")
        self.status(2, "downloading")
        self.working(2, "downloading", 42.0)
        self.status(3, "searching")
        self.working(3, "searching YouTube Music")
        self.status(4, "review")  # needs a match picked: no longer holds the batch open, but counts
        p = self.d.progress(1)
        self.assertEqual((p["total"], p["ready"], p["waiting"], p["stuck"], p["later"]), (5, 1, 1, 1, 0))
        self.assertEqual([(a["title"], a["stage"], a["percent"]) for a in p["active"]],
                         [("Song 2", "downloading", 42.0), ("Song 3", "finding", 0.0)])
        self.assertEqual(p["next"], {"title": "Song 5", "artists": ["Nova Band"]})
        self.assertEqual(p["eta"], int(3 * 3600 / 250))  # three on their way, at 250 an hour
        self.assertIsNone(p["paused"])
        self.assertIsNone(self.d.progress(2))  # someone else's

        # the batch keeps its start as its first songs arrive: "3 of 5", not "1 of 3"
        for n in (2, 3):
            self.done(n)
        p = self.d.progress(1)
        self.assertEqual((p["total"], p["ready"]), (5, 3))
        self.request(6)  # more while it runs: the same batch, bigger
        self.assertEqual(self.d.progress(1)["total"], 6)
        self.status(5, "failed", next_attempt=time.time() + 3600)  # tried again in an hour: doesn't hold it open
        self.done(6)
        self.assertIsNone(self.d.progress(1))
        self.request(7)  # a new batch
        p = self.d.progress(1)
        self.assertEqual((p["total"], p["ready"]), (1, 0))

    def test_paused_says_why_and_until_when(self):
        self.request(1)
        self.svc.cooldown_until = self.clock + 600
        p = self.d.progress(1)
        self.assertEqual(p["paused"], {"reason": "YouTube asked Songarr to slow down", "until": self.clock + 600})
        self.assertEqual(p["eta"], int(3600 / 250 + 600))
        self.svc.cooldown_until = 0
        self.svc.paused = True
        p = self.d.progress(1)
        self.assertEqual((p["paused"]["reason"], p["eta"]), ("Downloads are paused on the server", None))

    def test_one_song_is_followed_through_each_stage(self):
        self.request(1, title="Hit One")
        self.d.check()
        self.assertEqual([(m["kind"], m["title"], m["body"], m["urgent"]) for m in self.sent],
                         [("progress", "Hit One is waiting to download", "After the songs ahead of it", True)])
        self.assertEqual((self.sent[0]["to"], self.sent[0]["type"], self.sent[0]["indeterminate"]), ([1], "downloads", 1))
        self.status(1, "searching")
        self.working(1, "searching YouTube")
        self.clock += 5
        self.d.check()
        self.assertEqual(self.sent[-1]["title"], "Finding Hit One on YouTube")
        self.status(1, "downloading")
        self.working(1, "downloading", 50.0)
        self.clock += 5
        self.d.check()
        last = self.sent[-1]
        self.assertEqual((last["title"], last["body"], last["ready"], last["total"], last["indeterminate"], last["urgent"]),
                         ("Downloading Hit One", "Nova Band", 50, 100, 0, False))
        self.clock += 1
        self.working(1, "downloading", 90.0)
        self.d.check()
        self.assertEqual(len(self.sent), 3)  # (not every second)
        self.done(1)
        self.clock += 5
        self.d.check()
        last = self.sent[-1]
        self.assertEqual((last["kind"], last["title"], last["body"], last["urgent"], last["ttl"]),
                         ("done", "Hit One is ready", "Nova Band · on your server", True, 86400))
        self.assertEqual(self.svc.releases.notifications(1)["notifications"], [])  # one song: not worth a note in the bell
        self.d.check()
        self.assertEqual(len(self.sent), 4)  # and then quiet
        self.assertLess(self.sent[0]["at"], self.sent[-1]["at"])  # (the phone keeps the newest)

    def test_a_big_batch_updates_once_a_minute_and_ends_with_a_note(self):
        self.request(*range(1, 11))
        self.status(10, "downloading")
        self.working(10, "downloading", 10.0)
        self.d.check()
        self.assertEqual((self.sent[-1]["title"], self.sent[-1]["body"], self.sent[-1]["ready"], self.sent[-1]["total"]),
                         ("Downloading 10 songs", "Now: Song 10 · about 2 min left", 0, 10))
        for n in (10, 9):
            self.done(n)
            self.status(n - 1, "downloading")
            self.working(n - 1, "downloading", 50.0)
            self.clock += 20
            self.d.check()
        self.assertEqual(len(self.sent), 1)  # songs arriving, but less than a minute on
        self.clock += 30
        self.d.check()
        self.assertEqual((self.sent[-1]["title"], self.sent[-1]["body"], self.sent[-1]["ready"], self.sent[-1]["urgent"]),
                         ("2 of 10 songs ready", "Now: Song 8", 2, False))
        self.svc.active.clear()  # (nothing runs while YouTube wants a break)
        self.status(8, "wanted")
        self.svc.cooldown_until = self.clock + 1800
        self.d.check()  # a pause is news straight away
        self.assertEqual((self.sent[-1]["stage"], self.sent[-1]["title"], self.sent[-1]["urgent"]), ("paused", "Waiting to download 8 songs", True))
        self.assertIn("YouTube asked Songarr to slow down; carrying on at ", self.sent[-1]["body"])
        self.svc.cooldown_until = 0
        self.d.check()
        self.assertEqual(self.sent[-1]["title"], "8 songs waiting to download")
        for n in range(1, 8):
            self.done(n)
        self.status(8, "review")
        self.d.check()
        last = self.sent[-1]
        self.assertEqual((last["kind"], last["title"], last["body"], last["ready"], last["total"]),
                         ("done", "9 of 10 songs are ready", "1 needs a hand (see Requested songs)", 9, 10))
        note = self.svc.releases.notifications(1)["notifications"][0]
        self.assertEqual((note["kind"], note["title"], note["requests"]), ("downloads", "9 of 10 songs are ready", True))

    def test_all_ready_and_taken_out_again(self):
        self.request(1, 2)
        self.d.check()
        self.done(1)
        self.done(2)
        self.d.check()
        self.assertEqual((self.sent[-1]["title"], self.sent[-1]["body"]), ("Your songs are ready", "2 songs on your server"))
        self.request(3)
        self.d.check()
        self.svc.db.run("UPDATE tracks SET monitored = 0 WHERE id = ?", (tid(3),))  # removed from their library before it came
        self.d.check()
        self.assertEqual(self.sent[-1]["kind"], "clear")

    def test_asking_again_is_a_new_request(self):
        self.request(1)
        self.done(1)
        self.request(2)
        self.done(2)
        self.clock += 60
        self.request(1)  # again: at the top of the list
        ids = [r[0] for r in self.svc.db.q("SELECT track_id FROM requests WHERE user_id = 1 ORDER BY created DESC")]
        self.assertEqual(ids, [tid(1), tid(2)])

    def test_the_app_asks(self):
        port = free_port()
        http = make_app_server(self.svc, "127.0.0.1", port)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            code, _ = self.svc.users.new_pairing_code(1)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/auth/pair", method="POST",
                                         data=json.dumps({"code": code, "device": "Phone"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                token = json.load(r)["token"]

            def get(path):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1{path}", headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.load(r)

            self.assertEqual(get("/requests/progress"), {"progress": None})
            self.request(1, 2)
            self.status(1, "downloading")
            self.working(1, "converting", 100.0)
            p = get("/requests/progress")["progress"]
            self.assertEqual((p["total"], p["ready"], p["waiting"], p["active"][0]["stage"]), (2, 0, 1, "finishing"))
            self.assertNotIn("since", p)
            listed = get("/requests")
            self.assertEqual(([t["id"] for t in listed["requests"]], listed["progress"]["total"]), ([tid(2), tid(1)], 2))
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
