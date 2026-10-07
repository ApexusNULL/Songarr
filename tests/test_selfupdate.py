"""Songarr updating itself from its git repository (against local repositories: no network)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from songarr.selfupdate import SelfUpdate, head_commit
from songarr.service import Service

from tests.helpers import FakeYouTube

GIT = shutil.which("git")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "core.autocrlf=false",
                        *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@unittest.skipUnless(GIT, "git isn't installed")
class Repos(unittest.TestCase):
    """origin (as GitHub), a working copy that publishes to it, and a server's clone of it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.origin, self.work, self.server = self.dir / "origin.git", self.dir / "work", self.dir / "server"
        git(self.dir, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(self.dir, "clone", "-q", str(self.origin), str(self.work))
        self.publish({"pkgx/__init__.py": "VALUE = 1\n", "requirements.txt": "yt-dlp\n"}, "first")
        git(self.dir, "clone", "-q", str(self.origin), str(self.server))
        self.su = SelfUpdate(self.server, modules=["pkgx"])
        self.pip: list[list[str]] = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def publish(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text, encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        git(self.work, "push", "-q", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def install(self, specs):
        self.pip.append(specs)
        return True, "ok"

    def value(self) -> str:
        return (self.server / "pkgx" / "__init__.py").read_text(encoding="utf-8").strip()


class SelfUpdateTests(Repos):
    def test_new_commits_are_taken(self):
        self.assertEqual(self.su.check()["behind"], 0)
        new = self.publish({"pkgx/__init__.py": "VALUE = 2\n"}, "Make it two")
        state = self.su.check()
        self.assertEqual((state["behind"], state["blocked"], state["error"]), (1, None, None))
        done = self.su.update(self.install)
        self.assertEqual((done["updated"], done["to"], done["count"], done["subject"]), (True, new[:7], 1, "Make it two"))
        self.assertEqual((self.value(), head_commit(self.server)), ("VALUE = 2", new))
        self.assertEqual(self.pip, [])  # requirements.txt didn't change

    def test_new_requirements_are_installed(self):
        self.publish({"requirements.txt": "yt-dlp\npillow\n"}, "Needs Pillow")
        self.su.check()
        self.assertTrue(self.su.update(self.install)["updated"])
        self.assertEqual(self.pip, [["-r", str(self.server / "requirements.txt")]])

    def test_changes_of_its_own_are_never_overwritten(self):
        self.publish({"pkgx/__init__.py": "VALUE = 2\n"}, "Make it two")
        (self.server / "pkgx" / "__init__.py").write_text("VALUE = 'mine'\n", encoding="utf-8")
        state = self.su.check()
        self.assertEqual(state["behind"], 1)
        self.assertIn("changes of its own", state["blocked"])
        self.assertEqual(self.value(), "VALUE = 'mine'")

    def test_a_version_that_doesnt_start_is_put_back(self):
        old = head_commit(self.server)
        broken = self.publish({"pkgx/__init__.py": "VALUE = (\n"}, "Oops")
        self.su.check()
        done = self.su.update(self.install)
        self.assertEqual((done["updated"], done["failed"]), (False, broken[:7]))
        self.assertEqual((head_commit(self.server), self.value()), (old, "VALUE = 1"))  # the one that works
        self.assertIn("didn't start", self.su.check(failed=done["failed"])["blocked"])  # not tried again
        fixed = self.publish({"pkgx/__init__.py": "VALUE = 3\n"}, "Fixed")
        self.assertIsNone(self.su.check(failed=done["failed"])["blocked"])  # the next one is
        self.assertTrue(self.su.update(self.install)["updated"])
        self.assertEqual(head_commit(self.server), fixed)

    def test_not_a_clone(self):
        plain = SelfUpdate(self.dir / "plain", modules=["pkgx"])
        (self.dir / "plain").mkdir()
        self.assertFalse(plain.info()["git"])
        self.assertEqual(plain.check()["behind"], 0)


class UpdateCheckTests(Repos):
    """Through the update check (dependencies.py): history, and a restart once nobody's listening."""

    def test_the_update_check_updates_songarr_too(self):
        svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        try:
            deps = svc.dependencies
            deps.selfupdate = self.su
            deps.installed_versions = lambda: {}
            new = self.publish({"pkgx/__init__.py": "VALUE = 2\n"}, "Make it two")
            result = deps.check(install=True)
            self.assertEqual(result["found"]["Songarr"]["to"], new[:7])
            self.assertTrue(deps.state["restart_pending"])
            self.assertIn("Updated Songarr", svc.db.one("SELECT message FROM history WHERE event = 'updated'")[0])
            info = deps.info()["songarr"]
            self.assertEqual((info["git"], info["commit"], info["behind"]), (True, new[:7], 0))
            # a broken one is put back and remembered
            deps.state["restart_pending"] = False
            self.publish({"pkgx/__init__.py": "VALUE = (\n"}, "Oops")
            result = deps.check(install=True)
            self.assertNotIn("Songarr", result["found"])
            self.assertIn("didn't start", result["error"])
            self.assertFalse(deps.state["restart_pending"])
            self.assertTrue(svc.db.setting("selfupdate_failed"))
        finally:
            svc.stop(wait=True)


if __name__ == "__main__":
    unittest.main()
