"""Rebuilding the Android app with the chosen name and icon, and offering it to phones as an update.

Phones show the name and icon inside the app as soon as they're changed. The app's own icon and
name on the home screen are part of the app itself, so they need a new build: this runs
`flutter build apk` on this PC (it needs the app's source folder, Flutter, and the signing key the
phones' app was built with), then publishes the result like any other app update.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import app_updates

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

BUILD_TIMEOUT = 45 * 60
OVERLAY = Path("android") / "app" / "src" / "release" / "res"  # release-only resources: they win over src/main


def find_flutter(configured: str = "") -> str | None:
    for candidate in (configured, shutil.which("flutter"), r"C:\src\flutter\bin\flutter.bat",
                      str(Path.home() / "flutter" / "bin" / "flutter.bat")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def bump_build(app_dir: Path, at_least: int) -> tuple[str, int]:
    """Raise pubspec.yaml's build number past [at_least] (phones install only newer builds)."""
    path = app_dir / "pubspec.yaml"
    text = path.read_text(encoding="utf-8")
    version, build = app_updates.pubspec_version(app_dir)
    build = max(build, at_least) + 1
    path.write_text(re.sub(r"^(version:\s*[0-9][0-9.]*)\+\d+", rf"\g<1>+{build}", text, count=1, flags=re.M),
                    encoding="utf-8")
    return version, build


class AppBuild:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.state: dict = {"running": False, "step": None, "started": None, "finished": None, "ok": None,
                            "message": None, "log": ""}
        self._lock = threading.Lock()
        self.run_build = self._flutter  # swapped out in tests

    def app_dir(self) -> Path:
        configured = self.db.setting("app_source_dir") or ""
        return Path(configured) if configured else app_updates.PROGRAM_DIR / "app"

    def signature(self) -> str:
        b = self.svc.branding
        return f"{b.name()}|{b.icon_id()}|{b.background() if b.icon_id() else ''}"

    def check(self) -> dict:
        """Can this PC rebuild the app? With what's missing if not."""
        app = self.app_dir()
        problems = []
        if not (app / "pubspec.yaml").is_file():
            problems.append(f"The app's source folder wasn't found ({app}). Set it below.")
        flutter = find_flutter(self.db.setting("flutter_path") or "")
        if not flutter:
            problems.append("Flutter isn't installed (or isn't on the PATH).")
        if (app / "pubspec.yaml").is_file() and not (app / "android" / "key.properties").is_file():
            problems.append("The app's signing key (android/key.properties) is missing; phones only "
                            "install updates signed with the key their app was built with.")
        built = self.db.setting("app_brand_built") or "Songarr||"
        return {"ready": not problems, "problems": problems, "app_dir": str(app), "flutter": flutter,
                "needed": built != self.signature()} | {k: v for k, v in self.state.items() if k != "log"} | {
                "log": self.state["log"] if self.state["ok"] is False else ""}

    def start(self) -> None:
        with self._lock:
            if self.state["running"]:
                raise ValueError("The app is already being rebuilt.")
            check = self.check()
            if not check["ready"]:
                raise ValueError(" ".join(check["problems"]))
            self.state.update(running=True, step="Starting", started=time.time(), finished=None, ok=None,
                              message=None, log="")
        threading.Thread(target=self._run, name="app-build", daemon=True).start()

    def _run(self) -> None:
        app, b = self.app_dir(), self.svc.branding
        signature, name = self.signature(), b.name()
        try:
            self.state["step"] = "Putting in the name and icon"
            b.write_android(app / OVERLAY)
            published = max((e.get("build", 0) for e in self.svc.app_updates.manifest().get("files", {}).values()), default=0)
            version, build = bump_build(app, published)
            self.state["step"] = f"Building version {version} ({build}); this takes a few minutes"
            ok, out = self.run_build(app, name)
            self.state["log"] = out[-4000:]
            if not ok:
                raise RuntimeError("The build failed. The last lines of its output are below.")
            self.state["step"] = "Offering it to phones"
            app_updates.publish(self.svc.data_dir, app, notes=f"The app is now called {name}, with its new icon."
                                if name != "Songarr" or b.icon_id() else "Back to the Songarr name and icon.")
            self.db.set_setting("app_brand_built", signature)
            self.db.log("app-build", f"Rebuilt the app as {name} (version {version}, build {build}); phones are offered the update.")
            self.state.update(ok=True, message=f"Version {version} ({build}) is ready; phones offer it on their next check.")
        except (Exception, SystemExit) as e:
            log.exception("app rebuild failed")
            self.db.log("app-build", f"Couldn't rebuild the app: {e}")
            self.state.update(ok=False, message=str(e))
        finally:
            self.state.update(running=False, step=None, finished=time.time())
            self.db.release()

    def _flutter(self, app: Path, name: str) -> tuple[bool, str]:
        flutter = find_flutter(self.db.setting("flutter_path") or "")
        defines = Path(tempfile.mkdtemp(prefix="songarr-build-")) / "brand.json"
        defines.write_text(json.dumps({"BRAND_NAME": name}), encoding="utf-8")  # a file: no quoting on the command line
        try:
            r = subprocess.run([flutter, "build", "apk", "--release", "--split-per-abi", f"--dart-define-from-file={defines}"],
                               cwd=app, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=BUILD_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               env=os.environ | {"CI": "true"})
            return r.returncode == 0, r.stdout + r.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        finally:
            shutil.rmtree(defines.parent, ignore_errors=True)
