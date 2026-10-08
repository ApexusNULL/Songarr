"""Automatic updates of yt-dlp and Songarr's other packages: which releases qualify, installing with
a rollback when the new version doesn't import, and restarting only when nobody's listening."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from songarr import dependencies
from songarr.dependencies import newest_allowed, parse
from songarr.service import Service

from tests.helpers import FakeYouTube

NOW = 2_000_000_000.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(ts))


def release(age_hours: float, yanked: bool = False) -> list[dict]:
    return [{"upload_time_iso_8601": iso(NOW - age_hours * 3600), "yanked": yanked}]


class PolicyTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(parse("2026.8.19"), (2026, 8, 19))
        self.assertIsNone(parse("2026.9.1.dev0"))
        self.assertIsNone(parse("2.0rc1"))
        self.assertIsNone(parse("1.0+local"))

    def test_which_release_is_installed(self):
        data = {"releases": {
            "2026.8.19": release(500),
            "2026.9.30": release(30),
            "2026.10.6": release(1),  # too new: waits a couple of hours in case it's pulled
            "2026.10.1": release(10, yanked=True),
            "2026.11.1.dev0": release(50),  # nightly: never
        }}
        self.assertEqual(newest_allowed("yt-dlp", "2026.8.19", "any", NOW, data), "2026.9.30")
        self.assertIsNone(newest_allowed("yt-dlp", "2026.9.30", "any", NOW, data))  # already current

    def test_other_packages_stay_on_their_major_version(self):
        data = {"releases": {"17.2": release(100), "17.4": release(50), "18.0": release(40)}}
        self.assertEqual(newest_allowed("websockets", "17.2", "major", NOW, data), "17.4")
        self.assertEqual(newest_allowed("websockets", "17.2", "any", NOW, data), "18.0")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.deps = self.svc.dependencies
        self.versions = {"yt-dlp": "2026.8.19", "ytmusicapi": "1.12.3", "mutagen": "1.48.1", "websockets": "17.2", "segno": "1.6.6"}
        self.deps.installed_versions = lambda: dict(self.versions)
        self.deps.newest_allowed = lambda name, have, policy: {"yt-dlp": "2026.9.30"}.get(name)
        self.installs: list[list[str]] = []

        def pip(specs):
            self.installs.append(specs)
            for spec in specs:
                name, ver = spec.split("==")
                self.versions[name.split("[")[0]] = ver
            return True, "ok"
        self.deps.pip_install = pip

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_installs_the_new_release_and_waits_to_restart(self):
        self.deps.imports_ok = lambda modules: (True, "")
        result = self.deps.check()
        self.assertEqual(result["found"], {"yt-dlp": {"from": "2026.8.19", "to": "2026.9.30"}})
        self.assertEqual(self.installs, [["yt-dlp[default]==2026.9.30"]])
        self.assertTrue(self.deps.state["restart_pending"])
        self.assertIn("yt-dlp 2026.8.19 → 2026.9.30", self.svc.db.one("SELECT message FROM history WHERE event = 'updated'")[0])

    def test_a_broken_release_is_rolled_back_and_not_tried_again(self):
        self.deps.imports_ok = lambda modules: (False, "ImportError")
        self.deps.check()
        self.assertEqual(self.installs, [["yt-dlp[default]==2026.9.30"], ["yt-dlp[default]==2026.8.19"]])  # put back
        self.assertEqual(self.versions["yt-dlp"], "2026.8.19")
        self.assertFalse(self.deps.state["restart_pending"])
        self.assertEqual(self.svc.db.setting("update_failed"), ["yt-dlp==2026.9.30"])
        self.installs.clear()
        self.assertEqual(self.deps.check()["found"], {})  # that release is skipped from now on
        self.assertEqual(self.installs, [])

    def test_restarts_only_when_nobody_is_listening(self):
        restarted = []
        self.svc.restart_hook = lambda: restarted.append(True)
        self.svc.last_app_request = time.time()  # a phone is using it right now
        self.assertFalse(self.deps.restart_when_quiet())
        self.svc.last_app_request = time.time() - dependencies.QUIET_FOR - 1
        jam = self.svc.jams.start(1, [{"id": "s1"}], 0, 0, True, [])  # a Jam is on
        self.assertFalse(self.deps.restart_when_quiet())
        self.svc.jams.leave(jam.id, 1)
        self.assertTrue(self.deps.restart_when_quiet())
        self.assertEqual(restarted, [True])
        self.assertTrue(self.svc.draining)  # no new downloads were started meanwhile

    def test_one_check_at_a_time(self):
        started, go = threading.Event(), threading.Event()

        def slow_pip(specs):
            started.set()
            go.wait(5)
            return True, "ok"
        self.deps.pip_install = slow_pip
        self.deps.imports_ok = lambda modules: (True, "")
        first = threading.Thread(target=self.deps.check)
        first.start()
        self.assertTrue(started.wait(5))
        self.assertIn("Already checking", self.deps.check()["error"])  # "Check now" during the scheduled check
        go.set()
        first.join(5)
        self.assertNotIn("Already checking", self.deps.check()["error"] or "")

    def test_a_standby_server_keeps_itself_up_to_date(self):
        self.svc.start_standby()
        self.assertEqual([t.name for t in self.svc.threads], ["dependencies"])  # (and nothing else: no downloads)
        self.assertTrue(self.svc.threads[0].is_alive())

    def test_can_be_switched_off(self):
        self.svc.db.set_setting("auto_update", False)
        self.assertFalse(self.deps.info()["enabled"])


if __name__ == "__main__":
    unittest.main()
