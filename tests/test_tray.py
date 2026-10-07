"""The icon by the clock: what its menu shows, and what each item does."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from songarr import dependencies
from songarr.service import Service
from songarr.tray import ICON, Tray, status_line

from tests.helpers import FakeYouTube, spotify_track


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.svc.db.set_setting("library_root", str(self.dir))
        self.calls: list[str] = []
        self.answer = True
        self.notes: list[tuple[str, str]] = []
        self.tray = Tray(self.svc, "http://127.0.0.1:8484", self.dir / "logs",
                         stop=lambda: self.calls.append("stop"), restart=lambda: self.calls.append("restart"))
        self.tray.open_url = lambda url: self.calls.append("open " + url)
        self.tray.open_folder = lambda path: self.calls.append("folder " + path)
        self.tray.ask = lambda title, text: self.calls.append("ask " + title) or self.answer
        self.tray.notify = lambda title, text: self.notes.append((title, text))
        deps = self.svc.dependencies
        deps.installed_versions = lambda: {"yt-dlp": "2026.8.19"}
        deps.newest_allowed = lambda name, have, policy: None
        deps.imports_ok = lambda modules: (True, "")
        deps.pip_install = lambda specs: (True, "")

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def labels(self) -> list[str]:
        return [i.label if i else "---" for i in self.tray.items()]

    def test_the_icon_ships_with_songarr(self):
        self.assertTrue(ICON.exists())

    def test_menu(self):
        self.assertEqual(self.labels(), [
            "Open Songarr", "---", "Up to date · 0 songs", "Pause downloads", "Check for updates", "Restart Songarr",
            "---", "Open music folder", "Open log folder", "---", "Stop Songarr"])
        self.assertTrue(self.tray.items()[0].default)
        self.assertFalse(self.tray.items()[2].enabled)  # the status line is just information

    def test_status_line(self):
        self.svc.db.upsert_tracks([spotify_track(n) for n in range(1, 4)])
        self.assertEqual(status_line(self.svc), "3 songs waiting")
        self.svc.active["x"] = {}
        self.assertEqual(status_line(self.svc), "Downloading 1 · 3 waiting")
        self.svc.cooldown_until = time.time() + 600
        self.assertTrue(status_line(self.svc).startswith("YouTube break until "))
        self.svc.paused = True
        self.assertEqual(status_line(self.svc), "Downloads paused")
        self.assertIn("Resume downloads", self.labels())
        self.assertEqual(self.tray.tip(), "Songarr · Downloads paused")

    def test_pause_resume_and_open(self):
        self.tray.act("pause")
        self.assertTrue(self.svc.paused)
        self.tray.act("resume")
        self.assertFalse(self.svc.paused)
        self.tray.act("open")
        self.tray.act("library")
        self.tray.act("logs")
        self.assertEqual(self.calls, ["open http://127.0.0.1:8484", "folder " + str(self.dir), "folder " + str(self.dir / "logs")])

    def test_stop_asks_first(self):
        self.answer = False
        self.tray.act("stop")
        self.assertEqual(self.calls, ["ask Stop Songarr?"])
        self.answer = True
        self.tray.act("stop")
        self.assertEqual(self.calls[-1], "stop")
        self.assertIn("Stopped from the tray icon", self.svc.db.one("SELECT message FROM history WHERE event = 'system'")[0])

    def test_restart_asks_only_when_someone_is_listening(self):
        self.svc.last_app_request = 0
        self.tray.act("restart")
        self.assertEqual(self.calls, ["restart"])
        self.svc.last_app_request = time.time()  # a phone is playing
        self.answer = False
        self.tray.act("restart")
        self.assertEqual(self.calls, ["restart", "ask Restart Songarr?"])

    def test_check_for_updates(self):
        self.tray.act("update")
        self.assertEqual(self.notes, [("Everything is up to date", "yt-dlp 2026.8.19")])
        self.svc.dependencies.newest_allowed = lambda name, have, policy: "2026.9.30" if name == "yt-dlp" else None
        self.tray.act("update")
        self.assertEqual(self.notes[-1][0], "Updated yt-dlp 2026.9.30")
        self.assertIn("Restart to finish updating", self.labels())

    def test_a_failed_update_says_so(self):
        self.svc.dependencies.newest_allowed = lambda name, have, policy: "2026.9.30" if name == "yt-dlp" else None
        self.svc.dependencies.imports_ok = lambda modules: (False, "ImportError")
        self.tray.act("update")
        self.assertEqual(self.notes[-1][0], "Update didn't work")
        self.assertEqual(dependencies.PYPI, "http://127.0.0.1:9")  # never PyPI from the tests


if __name__ == "__main__":
    unittest.main()
