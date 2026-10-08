"""A backup server: two Songarr servers sharing the music on a NAS, one active at a time
(a Windows PC and an Ubuntu box, say, simulated here with two data folders and a shared folder)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr import cluster
from songarr.appapi import make_app_server
from songarr.cluster import Cluster, ClusterConfig, translate, translate_db
from songarr.db import DB
from songarr.service import Service
from songarr.web import _forwarded, make_server

from tests.helpers import FakeYouTube, spotify_track

WIN_MUSIC = "D:\\Music"
LINUX_MUSIC = "/mnt/nas/Music"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PathTests(unittest.TestCase):
    maps = [(WIN_MUSIC, LINUX_MUSIC), ("C:\\ProgramData\\Songarr", "/var/lib/songarr")]

    def test_windows_to_linux_and_back(self):
        song = WIN_MUSIC + "\\Daft Punk\\Discovery (2001)\\01 - One More Time.m4a"
        moved = translate(song, self.maps)
        self.assertEqual(moved, LINUX_MUSIC + "/Daft Punk/Discovery (2001)/01 - One More Time.m4a")
        back = [(d, s) for s, d in self.maps]
        self.assertEqual(translate(moved, back), song)
        self.assertEqual(translate("d:/music/A/b.m4a", self.maps), LINUX_MUSIC + "/A/b.m4a")  # Windows ignores case
        self.assertEqual(translate("C:\\ProgramData\\Songarr\\youtube-cookies.txt", self.maps), "/var/lib/songarr/youtube-cookies.txt")

    def test_only_whole_folders(self):
        for path in ("D:\\MusicVideos\\a.mp4", "D:\\Other\\a.m4a", "", "/mnt/nas/Music/a.m4a"):
            self.assertEqual(translate(path, self.maps), path)
        self.assertEqual(translate("/mnt/nas/music/a.m4a", [(d, s) for s, d in self.maps]),
                         "/mnt/nas/music/a.m4a")  # Linux paths keep their case

    def test_a_whole_database(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            svc = Service(tmp / "data", free_port(), youtube_factory=lambda s: FakeYouTube())
            svc.db.upsert_tracks([spotify_track(1)])
            svc.db.set_status(f"{1:022d}", "downloaded", file_path=WIN_MUSIC + "\\Artist 1\\Album 1 (2020)\\01 - Song 1.m4a")
            svc.db.run("INSERT INTO library_files(path, size, mtime, title_key) VALUES (?, 1, 1, 'song 1')", (WIN_MUSIC + "\\a.m4a",))
            svc.db.set_setting("cookies_file", "C:\\ProgramData\\Songarr\\youtube-cookies.txt")
            svc.stop(wait=True)
            source = {"library_root": WIN_MUSIC, "podcast_root": "D:\\Podcasts", "data_dir": "C:\\ProgramData\\Songarr"}
            here = {"library_root": LINUX_MUSIC, "podcast_root": "/mnt/nas/Podcasts", "data_dir": "/var/lib/songarr"}
            self.assertEqual(translate_db(tmp / "data" / "songarr.db", source, here), 3)
            con = sqlite3.connect(tmp / "data" / "songarr.db")
            self.assertEqual(con.execute("SELECT file_path FROM tracks").fetchone()[0], LINUX_MUSIC + "/Artist 1/Album 1 (2020)/01 - Song 1.m4a")
            self.assertEqual(con.execute("SELECT path FROM library_files").fetchone()[0], LINUX_MUSIC + "/a.m4a")
            self.assertEqual(json.loads(con.execute("SELECT value FROM settings WHERE key = 'cookies_file'").fetchone()[0]),
                             "/var/lib/songarr/youtube-cookies.txt")
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_which_admin_requests_a_standby_passes_on(self):
        self.assertTrue(_forwarded("/api/status", post=False))
        self.assertTrue(_forwarded("/api/settings", post=True))
        self.assertTrue(_forwarded("/api/users/2/import", post=True))  # an Exportify import lands on the active server
        self.assertFalse(_forwarded("/api/cluster", post=False))
        self.assertFalse(_forwarded("/api/system/restart", post=True))  # restarts this one
        self.assertFalse(_forwarded("/", post=False))


class SharedFileTests(unittest.TestCase):
    def test_a_write_waits_for_a_reader(self):
        """On Windows a file being read can't be replaced (the admin page reading lease.json while the
        heartbeat writes it): the write waits for the reader instead of failing."""
        folder = Path(tempfile.mkdtemp())
        try:
            lease = folder / "lease.json"
            cluster._write_json(lease, {"beat": 1})
            reader = open(lease, encoding="utf-8")
            threading.Timer(0.3, reader.close).start()
            cluster._write_json(lease, {"beat": 2})
            self.assertEqual(cluster._read_json(lease), {"beat": 2})
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["lease.json"])  # no .part left behind
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class TurnOnTests(unittest.TestCase):
    def test_turning_on_keeps_this_servers_own_settings(self):
        """Settings → Servers → Turn on: this server's folders and address are kept as its own before
        it restarts into the cluster (where it may take in another server's database)."""
        root = Path(tempfile.mkdtemp())
        try:
            (root / "data").mkdir()
            svc = Service(root / "data", free_port(), youtube_factory=lambda s: FakeYouTube())
            svc.db.set_setting("library_root", LINUX_MUSIC)
            svc.db.set_setting("public_url", "https://box.example")
            http = make_server(svc, "127.0.0.1", svc.port)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{svc.port}/api/cluster/setup", method="POST",
                    data=json.dumps({"folder": str(root / "shared"), "name": "box", "role": "backup"}).encode(),
                    headers={"X-Songarr": "1", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.assertEqual(r.status, 200)
            finally:
                http.shutdown()
                http.server_close()
                svc.stop(wait=True)
            config = ClusterConfig(root / "data")
            self.assertEqual((config.folder, config.name, config.role), (str(root / "shared"), "box", "backup"))
            self.assertEqual(config.local["library_root"], LINUX_MUSIC)
            self.assertEqual(config.local["public_url"], "https://box.example")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class Node:
    """One server: a data folder, its Service and its part in the cluster."""

    def __init__(self, root: Path, shared: Path, name: str, role: str, music: str | None, own_address: bool = True):
        self.data = root / name
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = ClusterConfig(self.data)
        self.config.folder, self.config.name, self.config.role = str(shared), name, role
        self.config.local = ({"library_root": music} if music else {}) | (
            {"public_url": f"https://{name}.example"} if own_address else {})
        self.config.save()
        self.restarts: list[str] = []
        self.start()

    def start(self) -> str:
        """As __main__ does: decide the role, copy the latest snapshot, open the database."""
        self.cluster = Cluster(self.data, ClusterConfig(self.data))
        role = self.cluster.startup()
        if role == "standby":
            self.cluster.import_snapshot()
        self.port = free_port()
        self.svc = Service(self.data, self.port, youtube_factory=lambda s: FakeYouTube())
        self.cluster.attach(self.svc)
        self.svc.restart_hook = lambda: self.restarts.append(self.cluster.state)
        return role

    def restart(self) -> str:
        self.cluster.close()
        self.svc.stop(wait=True)
        return self.start()

    def stop(self):
        self.cluster._done = True
        self.cluster.close()
        self.svc.stop(wait=True)


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.shared = self.root / "nas" / "Songarr servers"
        self.saved = (cluster.TAKEOVER_AFTER, cluster.TAKE_PAUSE, cluster.SETTLE, cluster.FENCE_AFTER, cluster.TICK,
                      cluster.MAILBOX_KEEP, cluster._write_json)
        cluster.TAKEOVER_AFTER, cluster.TAKE_PAUSE, cluster.SETTLE = 0.3, 0.05, 0
        self.pc = Node(self.root, self.shared, "pc", "main", WIN_MUSIC)  # the first server starts things off
        self.box = None

    def tearDown(self):
        (cluster.TAKEOVER_AFTER, cluster.TAKE_PAUSE, cluster.SETTLE, cluster.FENCE_AFTER, cluster.TICK,
         cluster.MAILBOX_KEEP, cluster._write_json) = self.saved
        for n in (self.pc, self.box):
            if n:
                n.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def add_box(self):
        self.box = Node(self.root, self.shared, "box", "backup", LINUX_MUSIC)

    def song_path(self, node: Node) -> str:
        return node.svc.db.one("SELECT file_path FROM tracks WHERE id = ?", (f"{1:022d}",))[0]

    def lease(self) -> dict:
        return json.loads((self.shared / "lease.json").read_text())

    def nas_away(self) -> None:
        """Nothing can be written to the cluster folder (the NAS is away) until nas_back()."""
        def away(*_):
            raise OSError("[WinError 53] The network path was not found")
        cluster._write_json = away

    def nas_back(self) -> None:
        cluster._write_json = self.saved[-1]

    def test_the_backup_keeps_a_copy_and_takes_over(self):
        self.assertEqual(self.pc.cluster.state, "active")
        self.assertFalse(self.pc.svc.standby)
        db = self.pc.svc.db
        db.upsert_tracks([spotify_track(1)])
        db.set_status(f"{1:022d}", "downloaded", file_path=WIN_MUSIC + "\\Artist 1\\Album 1 (2020)\\01 - Song 1.m4a")
        alex = self.pc.svc.users.create("Alex")
        self.pc.cluster.tick()  # heartbeat + snapshot

        self.add_box()
        self.assertEqual(self.box.cluster.state, "standby")
        self.assertTrue(self.box.svc.standby)
        self.assertEqual(self.song_path(self.box), LINUX_MUSIC + "/Artist 1/Album 1 (2020)/01 - Song 1.m4a")  # its own path
        self.assertEqual(self.box.svc.db.setting("library_root"), LINUX_MUSIC)  # its own settings
        self.assertEqual(self.pc.svc.db.setting("library_root"), WIN_MUSIC)
        self.assertIsNotNone(self.box.svc.db.one("SELECT 1 FROM users WHERE id = ?", (alex,)))

        # changes reach the backup with the next snapshot
        self.pc.svc.users.rename(alex, "Alex B")
        self.pc.cluster._last_snapshot = 0
        self.pc.cluster.tick()
        self.box.cluster.tick()
        self.assertEqual(self.box.svc.db.one("SELECT name FROM users WHERE id = ?", (alex,))[0], "Alex B")
        self.assertEqual([n["name"] for n in self.box.cluster.nodes()], ["pc", "box"])  # the active one first
        self.assertEqual(self.box.cluster.servers(), ["https://pc.example", "https://box.example"])

        # the PC goes quiet: no heartbeat
        time.sleep(0.4)
        self.box.cluster.tick()
        self.assertEqual(self.box.restarts, ["standby"])  # restarts as the active server
        self.assertEqual(json.loads((self.shared / "lease.json").read_text())["node"], "box")
        self.assertEqual(self.box.restart(), "active")
        self.assertFalse(self.box.svc.standby)

        # the PC comes back: the lease is gone, so it restarts as a standby
        self.pc.cluster.tick()
        self.assertEqual(self.pc.restarts, ["standby"])
        self.assertEqual(self.pc.restart(), "standby")

    def test_a_backup_waits_for_the_main_server(self):
        """Set up out of order, a new backup never starts things off with its empty database."""
        shared = self.root / "nas2"
        box = Node(self.root / "elsewhere", shared, "box", "backup", LINUX_MUSIC)
        pc = None
        try:
            self.assertEqual(box.cluster.state, "standby")
            time.sleep(0.4)
            box.cluster.tick()
            self.assertEqual(box.restarts, [])  # nothing to take over with
            self.assertFalse((shared / "lease.json").exists())

            pc = Node(self.root / "elsewhere", shared, "pc", "main", WIN_MUSIC)  # the main server starts it off
            self.assertEqual(pc.cluster.state, "active")
            pc.svc.users.create("Alex")
            pc.cluster.tick()
            box.cluster.tick()
            self.assertEqual(box.svc.db.one("SELECT name FROM users")[0], "Alex")
            self.assertEqual(box.restarts, [])
        finally:
            for n in (box, pc):
                if n:
                    n.stop()

    def test_joining_keeps_the_servers_own_database(self):
        data = self.root / "box"
        data.mkdir(parents=True)
        own = DB(data / "songarr.db")
        own.set_setting("brand_name", "Old box")
        own.close()
        self.pc.cluster.tick()
        self.add_box()
        self.assertNotEqual(self.box.svc.db.setting("brand_name"), "Old box")  # the shared copy is in use
        kept = sqlite3.connect(data / "songarr-before-joining.db")
        self.assertEqual(kept.execute("SELECT value FROM settings WHERE key = 'brand_name'").fetchone()[0], '"Old box"')
        kept.close()
        self.pc.svc.users.create("Sam")  # later copies don't replace the kept one
        self.pc.cluster._last_snapshot = 0
        self.pc.cluster.tick()
        self.box.cluster.tick()
        kept = sqlite3.connect(data / "songarr-before-joining.db")
        self.assertIsNone(kept.execute("SELECT 1 FROM users WHERE name = 'Sam'").fetchone())
        kept.close()

    def test_a_joining_server_keeps_to_its_own_settings(self):
        """A backup without an app address of its own doesn't take the main server's (still in the main
        server's database from before it joined, so in the shared copy too)."""
        self.pc.svc.db.run("INSERT INTO settings(key, value) VALUES('public_url', ?)", (json.dumps("https://old.example"),))
        self.pc.cluster._last_snapshot = 0
        self.pc.cluster.tick()
        self.box = Node(self.root, self.shared, "box", "backup", LINUX_MUSIC, own_address=False)
        self.assertEqual(self.box.svc.db.setting("public_url"), "")
        self.assertEqual(self.box.svc.db.settings()["public_url"], "")
        self.assertNotIn("public_url", self.box.cluster.config.local)
        self.assertEqual(self.box.svc.db.setting("library_root"), LINUX_MUSIC)
        self.assertEqual(self.box.cluster.servers(), ["https://pc.example"])  # no address for the box yet
        self.assertEqual(self.pc.svc.db.setting("public_url"), "https://pc.example")

    def test_own_settings_are_saved_when_joining(self):
        """A server that had its folders and address before joining keeps them in cluster.json, so they
        survive taking in another server's copy later (after a failover)."""
        root = self.root / "solo"
        data = root / "main2"
        data.mkdir(parents=True)
        own = DB(data / "songarr.db")
        own.set_setting("library_root", WIN_MUSIC)
        own.set_setting("public_url", "https://main2.example")
        own.close()
        node = Node(root, root / "shared", "main2", "main", None, own_address=False)
        try:
            self.assertEqual(node.cluster.state, "active")
            saved = ClusterConfig(data).local
            self.assertEqual((saved.get("library_root"), saved.get("public_url")), (WIN_MUSIC, "https://main2.example"))
        finally:
            node.stop()

    def test_the_main_server_takes_back_over_when_nobody_is_listening(self):
        self.pc.svc.db.upsert_tracks([spotify_track(1)])
        self.pc.svc.db.set_status(f"{1:022d}", "downloaded", file_path=WIN_MUSIC + "\\A\\B\\01 - Song 1.m4a")
        self.pc.cluster.tick()
        self.add_box()
        self.pc.cluster._done = True  # the PC stops without a word
        time.sleep(0.4)
        self.box.cluster.tick()
        self.box.restart()  # the backup is active now
        self.box.svc.users.create("Made while the PC was away")
        self.box.cluster._last_snapshot = 0
        self.box.cluster.tick()

        self.pc.restart()  # back: a standby asking for the lease
        self.assertEqual(self.pc.cluster.state, "standby")
        self.pc.cluster.tick()  # asks for it
        self.assertEqual(json.loads((self.shared / "handover.json").read_text())["to"], "pc")
        self.box.cluster.tick()  # sees the PC is alive...
        self.pc.cluster.tick()
        self.box.cluster.tick()  # ...and nobody's listening: hands over
        self.assertEqual(json.loads((self.shared / "lease.json").read_text())["node"], "pc")
        self.assertEqual(len(self.box.restarts), 2)  # once to take over, once to stand by again
        self.pc.cluster.tick()  # the lease is the PC's: it takes the last copy and restarts as active
        self.assertEqual(self.pc.restarts, ["standby"])
        self.assertEqual(self.pc.restart(), "active")
        self.assertEqual(self.song_path(self.pc), WIN_MUSIC + "\\A\\B\\01 - Song 1.m4a")  # back to the PC's paths
        self.assertIsNotNone(self.pc.svc.db.one("SELECT 1 FROM users WHERE name = 'Made while the PC was away'"))

    def test_a_server_made_active_stays_active(self):
        self.pc.cluster.tick()
        self.add_box()
        self.box.cluster.tick()
        self.pc.cluster.tick()
        self.box.cluster.make_active()  # "Make active" for the box
        self.pc.cluster.tick()  # hands over at once, even with someone listening
        lease = json.loads((self.shared / "lease.json").read_text())
        self.assertEqual((lease["node"], lease["chosen"]), ("box", True))
        self.box.cluster.tick()
        self.box.restart()
        self.box.cluster.tick()
        self.pc.restart()  # the main server, standing by now
        self.pc.cluster.tick()
        self.assertFalse((self.shared / "handover.json").exists())  # doesn't ask for it back
        self.assertTrue(json.loads((self.shared / "lease.json").read_text())["chosen"])  # still chosen after heartbeats

    def test_no_hand_back_straight_after_taking_over(self):
        cluster.SETTLE = 600
        self.pc.cluster.tick()
        self.add_box()
        self.pc.cluster._done = True
        time.sleep(0.4)
        self.box.cluster.tick()
        self.box.restart()  # took over just now
        self.pc.restart()  # the main one is back and asks...
        self.pc.cluster.tick()
        self.box.cluster.tick()
        self.pc.cluster.tick()
        self.box.cluster.tick()
        self.assertEqual(json.loads((self.shared / "lease.json").read_text())["node"], "box")  # ...but not yet

    def test_stopping_the_active_server_hands_over_first(self):
        self.pc.cluster.tick()
        self.add_box()
        self.box.cluster.tick()
        self.pc.cluster.tick()  # each has seen the other's heartbeat
        self.pc.svc.users.create("Last change")
        self.pc.cluster.on_exit()
        self.assertEqual(json.loads((self.shared / "lease.json").read_text())["node"], "box")
        self.box.cluster.tick()
        self.assertEqual(self.box.restart(), "active")
        self.assertIsNotNone(self.box.svc.db.one("SELECT 1 FROM users WHERE name = 'Last change'"))  # nothing lost

    # -- when things go wrong ---------------------------------------------------------------------

    def test_no_takeover_the_moment_the_nas_is_back(self):
        """The NAS away for longer than the takeover time (rebooting, say): when it's back, the standby
        doesn't take over before the active server has had the chance to beat again."""
        self.pc.cluster.tick()
        self.add_box()
        self.box.cluster.tick()  # the standby saw the last heartbeat before the NAS went
        self.nas_away()
        for _ in range(3):
            time.sleep(0.15)
            for node in (self.pc, self.box):
                with self.assertRaises(OSError):
                    node.cluster.tick()
        self.nas_back()
        self.box.cluster.tick()  # back, and the standby happens to look first
        self.pc.cluster.tick()
        self.box.cluster.tick()
        self.assertEqual((self.lease()["node"], self.box.restarts, self.pc.restarts), ("pc", [], []))
        # a PC that's really gone: the box takes over once it has watched the takeover time
        self.pc.cluster._done = True
        time.sleep(0.4)
        self.box.cluster.tick()
        self.assertEqual((self.lease()["node"], self.box.restarts), ("box", ["standby"]))

    def hand_back(self) -> None:
        """The PC away, the box active and changing things, the PC back: the box hands back over."""
        self.pc.cluster.tick()
        self.add_box()
        self.pc.cluster._done = True
        time.sleep(0.4)
        self.box.cluster.tick()
        self.box.restart()  # the backup is active
        self.box.svc.users.create("Made while the PC was away")
        self.box.cluster._last_snapshot = 0
        self.box.cluster.tick()
        self.pc.restart()
        self.pc.cluster.tick()  # asks for the lease
        for node in (self.box, self.pc, self.box):  # the box hands over (with a last copy) once it sees the PC
            if self.lease()["node"] != "pc":
                node.cluster.tick()
        self.assertEqual((self.lease()["node"], self.pc.restarts), ("pc", []))
        self.assertEqual(self.box.restart(), "standby")

    def made_while_away(self, node: Node):
        return node.svc.db.one("SELECT 1 FROM users WHERE name = 'Made while the PC was away'")

    def test_a_hand_back_waits_for_the_last_copy(self):
        self.hand_back()

        def hiccup():
            raise OSError("the NAS hiccuped")
        self.pc.cluster.import_snapshot = hiccup
        self.pc.cluster.tick()
        self.assertEqual(self.pc.restarts, [])  # not active without the box's last copy
        del self.pc.cluster.import_snapshot  # (copying works again)
        self.pc.cluster.tick()
        self.assertEqual(self.pc.restarts, ["standby"])
        self.assertEqual(self.pc.restart(), "active")
        self.assertIsNotNone(self.made_while_away(self.pc))

    def test_the_backup_takes_the_lease_back_if_the_main_server_cant_copy_it(self):
        self.hand_back()

        def broken():
            raise OSError("no room left on this disk")
        self.pc.cluster.import_snapshot = broken
        self.pc.cluster.tick()
        self.box.cluster.tick()
        time.sleep(0.4)
        self.pc.cluster.tick()
        self.box.cluster.tick()  # the lease hasn't moved on: the box, with everything, takes it back
        self.assertEqual((self.lease()["node"], self.pc.restarts), ("box", []))
        self.assertEqual(self.box.restart(), "active")
        self.assertIsNotNone(self.made_while_away(self.box))

    def test_a_damaged_lease(self):
        """An empty lease.json (a power cut on the NAS, say): the active server writes it again; with
        nobody active, a standby takes over once it has stayed damaged for the takeover time."""
        self.pc.cluster.tick()
        self.add_box()
        (self.shared / "lease.json").write_text("")
        self.pc.cluster.tick()
        self.assertEqual(self.lease()["node"], "pc")
        (self.shared / "lease.json").write_text("")  # and then both servers restart
        self.assertEqual((self.pc.restart(), self.box.restart()), ("standby", "standby"))
        self.pc.cluster.tick()
        self.box.cluster.tick()
        time.sleep(0.4)
        self.pc.cluster.tick()
        self.assertEqual((self.lease()["node"], self.pc.restarts), ("pc", ["standby"]))
        self.assertEqual(self.pc.restart(), "active")

    def test_an_active_server_that_cant_renew_the_lease_stops_taking_changes(self):
        """A standby may be taking over meanwhile, and what this one took in would be lost."""
        cluster.FENCE_AFTER = 0.2
        self.pc.cluster.tick()
        self.nas_away()
        time.sleep(0.3)
        with self.assertRaises(OSError):
            self.pc.cluster.tick()
        self.pc.cluster._fence_if_stale()  # (a thread of its own does this every second)
        self.assertTrue(self.pc.svc.standby)  # the apps are told to go elsewhere
        self.assertTrue(self.pc.svc.paused)  # and no downloads start
        self.nas_back()
        self.pc.cluster.tick()  # the lease renewed: back to work
        self.assertEqual((self.pc.svc.standby, self.pc.svc.paused, self.pc.restarts), (False, False, []))

    def test_the_heartbeat_goes_on_during_a_long_snapshot(self):
        self.pc.cluster.tick()
        beats = []

        def watched(path, data):
            if path.name == "lease.json":
                beats.append(data["beat"])
            self.saved[-1](path, data)
        cluster._write_json, cluster.TICK = watched, 0  # (here every chunk copied takes "a while")
        self.pc.cluster.snapshot(force=True)
        self.assertGreater(len(beats), 1)
        self.assertEqual(beats, sorted(set(beats)))  # a heartbeat that keeps changing
        # another server took the lease meanwhile: the copy stops, and isn't offered to anyone
        version = json.loads((self.shared / "snapshot" / "meta.json").read_text())["version"]
        (self.shared / "lease.json").write_text(json.dumps({"node": "box", "epoch": 9, "beat": 0}))
        with self.assertRaises(cluster.ClusterError):
            self.pc.cluster.snapshot(force=True)
        self.assertEqual(json.loads((self.shared / "snapshot" / "meta.json").read_text())["version"], version)

    def test_a_standby_takes_over_even_if_the_last_snapshot_cant_be_copied(self):
        self.pc.cluster.tick()
        self.add_box()  # has the first copy
        self.pc.svc.users.create("Later")
        self.pc.cluster._last_snapshot = 0
        self.pc.cluster.tick()  # a newer copy, which turns out damaged (cut short)
        meta = json.loads((self.shared / "snapshot" / "meta.json").read_text())
        gz = self.shared / "snapshot" / meta["db"]
        gz.write_bytes(gz.read_bytes()[: gz.stat().st_size // 2])
        with self.assertRaises(OSError):  # (what starting up as a standby expects)
            self.box.cluster.import_snapshot()
        self.pc.cluster._done = True  # and then the PC goes quiet
        with self.assertLogs("songarr.cluster", "WARNING"):
            self.box.cluster.tick()
        time.sleep(0.4)
        self.box.cluster.tick()
        self.assertEqual((self.lease()["node"], self.box.restarts), ("box", ["standby"]))  # with the copy it has

    def test_the_mailbox_is_tidied_by_this_servers_own_clock(self):
        """Not by the times on the NAS, which may come from a clock that's hours out."""
        commands = self.shared / "commands"
        commands.mkdir(parents=True, exist_ok=True)
        request = commands / "1-2-3-abcdef.json"
        request.write_text("{}")
        os.utime(request, (time.time() - 7200,) * 2)
        self.pc.cluster._tidy_mailbox()
        self.assertTrue(request.exists())  # it only just arrived, as far as this server can tell
        cluster.MAILBOX_KEEP = 0.1
        time.sleep(0.15)
        self.pc.cluster._tidy_mailbox()
        self.assertFalse(request.exists())  # nobody collected it

    def test_the_newest_snapshot_is_kept_whatever_the_clocks(self):
        snapshots = self.shared / "snapshot"
        self.pc.cluster.tick()
        for version in (97, 98, 99):  # left by a server whose clock is an hour ahead
            old = snapshots / f"songarr-{version}.db.gz"
            old.write_bytes(b"")
            os.utime(old, (time.time() + 3600,) * 2)
        self.pc.svc.users.create("New")
        self.pc.cluster._last_snapshot = 0
        self.pc.cluster.tick()
        meta = json.loads((snapshots / "meta.json").read_text())
        self.assertTrue((snapshots / meta["db"]).exists())
        self.assertEqual(len(list(snapshots.glob("songarr-*.db.gz"))), cluster.KEEP_SNAPSHOTS)


class OneAdminPageTests(unittest.TestCase):
    """A standby's admin page shows and changes the active server's things; each server's own
    controls reach it from any admin page; the apps get pointed at the active server."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.shared = self.root / "shared"
        self.pc = Node(self.root, self.shared, "pc", "main", WIN_MUSIC)
        self.pc.cluster.tick()
        self.box = Node(self.root, self.shared, "box", "backup", LINUX_MUSIC)
        self.servers = []
        for node in (self.pc, self.box):
            http = make_server(node.svc, "127.0.0.1", node.port)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            threading.Thread(target=node.cluster.serve_commands, daemon=True).start()
            self.servers.append(http)
        self.app_port = free_port()
        self.app_http = make_app_server(self.box.svc, "127.0.0.1", self.app_port)
        threading.Thread(target=self.app_http.serve_forever, daemon=True).start()

    def tearDown(self):
        for http in (*self.servers, self.app_http):
            http.shutdown()
            http.server_close()
        self.pc.stop()
        self.box.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def admin(self, node, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{node.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Songarr": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_the_standby_admin_page_works_on_the_active_server(self):
        status, settings = self.admin(self.box, "GET", "/api/settings")  # the box is standing by
        self.assertEqual(status, 200)
        self.assertEqual(settings["library_root"], WIN_MUSIC)  # the PC's settings, from the PC
        status, _ = self.admin(self.box, "POST", "/api/settings", {"max_per_hour": 123})
        self.assertEqual(status, 200)
        self.assertEqual(self.pc.svc.db.setting("max_per_hour"), 123)  # changed on the PC
        status, info = self.admin(self.box, "GET", "/api/cluster")  # the box's own view
        self.assertEqual((info["name"], info["state"], info["active"]), ("box", "standby", "pc"))

    def test_each_servers_own_controls(self):
        status, r = self.admin(self.pc, "POST", "/api/cluster/command",
                               {"to": "box", "kind": "settings", "values": {"library_root": "/mnt/other/Music", "brand_name": "x"}})
        self.assertEqual(status, 200, r)
        self.assertEqual(ClusterConfig(self.box.data).local["library_root"], "/mnt/other/Music")  # saved on the box
        self.assertNotIn("brand_name", ClusterConfig(self.box.data).local)  # only its own settings
        status, r = self.admin(self.pc, "POST", "/api/cluster/command", {"to": "box", "kind": "make-active"})
        self.assertEqual((status, json.loads((self.shared / "handover.json").read_text())["to"]), (200, "box"))

    def test_the_apps_are_pointed_at_the_active_server(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.app_port}/api/v1/me", timeout=10)
            self.fail("a standby answers the app")
        except urllib.error.HTTPError as e:
            with e:
                body = json.loads(e.read())
            self.assertEqual((e.code, body["standby"], body["active"]), (503, True, "https://pc.example"))


if __name__ == "__main__":
    unittest.main()
