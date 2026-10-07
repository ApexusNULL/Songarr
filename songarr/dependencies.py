"""Keeps Songarr up to date: itself (from its git repository, see selfupdate.py) and its Python
packages, yt-dlp above all.

YouTube changes often and yt-dlp keeps up with frequent releases, so an old yt-dlp is the usual
reason downloads suddenly fail. Every few hours Songarr asks PyPI for new stable releases of the
packages it uses, installs them into its own environment, checks they import, and restarts itself
when nobody is using it.

What gets installed:
- yt-dlp and ytmusicapi (they talk to YouTube): every new stable release.
- the other packages: new stable releases with the same major version, so an incompatible change
  never arrives unattended.
- never pre-releases or yanked releases, and only releases at least SETTLE old (a release that is
  pulled again within hours for a bug is skipped).

If an update doesn't import, the previous versions are put back and that release isn't tried again.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .selfupdate import SelfUpdate

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

PYPI = "https://pypi.org/pypi"
# distribution -> (what to install, update policy, module to check it imports)
PACKAGES = {
    "yt-dlp": ("yt-dlp[default]", "any", "yt_dlp"),
    "ytmusicapi": ("ytmusicapi", "any", "ytmusicapi"),
    "mutagen": ("mutagen", "major", "mutagen"),
    "websockets": ("websockets", "major", "websockets"),
    "segno": ("segno", "major", "segno"),
    "pillow": ("pillow", "major", "PIL"),
}
FIRST_CHECK = 15 * 60  # after starting (and so after a restart for an update)
CHECK_EVERY = 6 * 3600
SETTLE = 2 * 3600
QUIET_FOR = 10 * 60  # no app activity for this long before restarting
DRAIN_MAX = 10 * 60  # longest wait for downloads in progress to finish
PIP_TIMEOUT = 15 * 60


def parse(v: str) -> tuple[int, ...] | None:
    """A final release's version as numbers; None for pre-, dev and local releases ("2.0rc1", "1.0.dev3")."""
    parts = v.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def _pypi(name: str) -> dict:
    with urllib.request.urlopen(f"{PYPI}/{name}/json", timeout=30) as r:
        return json.load(r)


def newest_allowed(name: str, installed: str, policy: str, now: float | None = None, data: dict | None = None) -> str | None:
    """The newest release of [name] Songarr would install over [installed], or None."""
    now = time.time() if now is None else now
    have = parse(installed)
    if have is None:
        return None
    data = data if data is not None else _pypi(name)
    best: tuple[tuple[int, ...], str] | None = None
    for v, files in (data.get("releases") or {}).items():
        num = parse(v)
        if num is None or num <= have or not files or any(f.get("yanked") for f in files):
            continue
        if policy == "major" and num[0] != have[0]:
            continue
        uploaded = min(datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")).timestamp() for f in files)
        if now - uploaded < SETTLE:
            continue
        if best is None or num > best[0]:
            best = (num, v)
    return best[1] if best else None


def installed_versions() -> dict[str, str]:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            pass
    return out


def _python() -> str:
    """python.exe beside pythonw.exe (pip prints, and pythonw has nowhere to print)."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)


def _run(args: list[str], timeout: float) -> tuple[bool, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([_python(), *args], capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def pip_install(specs: list[str]) -> tuple[bool, str]:
    return _run(["-m", "pip", "install", "--disable-pip-version-check", "--no-input", *specs], PIP_TIMEOUT)


def imports_ok(modules: list[str]) -> tuple[bool, str]:
    return _run(["-c", "import " + ", ".join(modules)], 120)


class Dependencies:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()
        self.state: dict = {"checked": None, "available": {}, "error": None, "updating": False, "restart_pending": False}
        # swapped out in tests
        self.newest_allowed: Callable[[str, str, str], str | None] = newest_allowed
        self.pip_install: Callable[[list[str]], tuple[bool, str]] = pip_install
        self.imports_ok: Callable[[list[str]], tuple[bool, str]] = imports_ok
        self.installed_versions: Callable[[], dict[str, str]] = installed_versions
        self.selfupdate = SelfUpdate()

    def info(self) -> dict:
        return ({"enabled": bool(self.db.setting("auto_update")), "installed": self.installed_versions()} | self.state
                | {"songarr": self.selfupdate.info()})

    def check(self, install: bool = True) -> dict:
        """Look for new releases and (if [install]) install them. Returns what was found and done."""
        found: dict[str, tuple[str, str]] = {}
        failed = set(self.db.setting("update_failed") or [])
        errors = []
        for name, (_, policy, _) in PACKAGES.items():
            have = self.installed_versions().get(name)
            if not have:
                continue
            try:
                new = self.newest_allowed(name, have, policy)
            except (OSError, ValueError, KeyError) as e:
                errors.append(f"{name}: {e}")
                continue
            if new and f"{name}=={new}" not in failed:
                found[name] = (have, new)
        self.state.update(checked=time.time(), available={k: v[1] for k, v in found.items()},
                          error="; ".join(errors)[:300] or None)
        if found and install:
            self._install(found)
        result = {"found": {k: {"from": a, "to": b} for k, (a, b) in found.items()}, "error": self.state["error"]}
        own = self.selfupdate.check(failed=self.db.setting("selfupdate_failed"))
        if own["behind"] and not own["blocked"] and install:
            done = self._update_self()
            if done.get("updated"):
                result["found"]["Songarr"] = {"from": done["from"], "to": done["to"]}
            elif done.get("error"):
                result["error"] = "; ".join(e for e in (result["error"], done["error"]) if e)[:300]
        return result

    def _update_self(self) -> dict:
        """Songarr's repository has new commits: move to them (selfupdate.py) and restart when quiet."""
        self.state["updating"] = True
        try:
            done = self.selfupdate.update(self.pip_install)
        finally:
            self.state["updating"] = False
        if done.get("updated"):
            what = f"{done['count']} change{'' if done['count'] == 1 else 's'}" + (f", the latest: {done['subject']}" if done.get("subject") else "")
            self.db.log("updated", f"Updated Songarr {done['from']} → {done['to']} ({what}). It restarts to use it when nobody's listening.")
            self.state["restart_pending"] = True
        elif done.get("failed"):
            self.db.set_setting("selfupdate_failed", done["failed"])
            self.db.log("update-failed", f"Couldn't update Songarr to {done['failed']}: {done['error']}")
        return done

    def _install(self, found: dict[str, tuple[str, str]]) -> None:
        self.state["updating"] = True
        try:
            specs = [f"{PACKAGES[n][0]}=={new}" for n, (_, new) in found.items()]
            log.info("updating %s", ", ".join(specs))
            ok, out = self.pip_install(specs)
            if ok:
                ok, out = self.imports_ok([m for _, _, m in PACKAGES.values()])
            if not ok:
                log.warning("update failed, putting back the previous versions: %s", out)
                self.pip_install([f"{PACKAGES[n][0]}=={old}" for n, (old, _) in found.items()])
                failed = set(self.db.setting("update_failed") or []) | {f"{n}=={new}" for n, (_, new) in found.items()}
                self.db.set_setting("update_failed", sorted(failed))
                self.db.log("update-failed", "Couldn't update " + ", ".join(f"{n} to {new}" for n, (_, new) in found.items())
                            + "; kept the versions that work")
                self.state["error"] = "The last update didn't work; the previous versions were kept."
                return
            self.db.log("updated", "Updated " + ", ".join(f"{n} {old} → {new}" for n, (old, new) in found.items())
                        + ". Songarr restarts to use it when nobody's listening.")
            self.state.update(available={}, restart_pending=True)
        finally:
            self.state["updating"] = False

    # -- the background loop ----------------------------------------------------------------

    def quiet(self) -> bool:
        """Nobody is using Songarr right now: no Jam, and no app activity for a while."""
        return self.svc.jams.count() == 0 and time.time() - self.svc.last_app_request > QUIET_FOR

    def restart_when_quiet(self) -> bool:
        """Finish the downloads in progress (starting no new ones), then restart. False if it isn't quiet."""
        hook = self.svc.restart_hook
        if hook is None or not self.quiet():
            return False
        self.svc.draining = True
        deadline = time.time() + DRAIN_MAX
        while self.svc.active and time.time() < deadline and not self.svc.stop_event.is_set():
            time.sleep(2)
        if not self.quiet():  # someone started listening meanwhile: try again later
            self.svc.draining = False
            return False
        log.info("restarting to use the updated packages")
        hook()
        return True

    def run(self) -> None:
        next_check = time.time() + FIRST_CHECK
        while not self.svc.stop_event.is_set():
            self.wake.wait(timeout=60)
            self.wake.clear()
            if self.svc.stop_event.is_set():
                break
            if self.state["restart_pending"] and self.restart_when_quiet():
                break
            if time.time() >= next_check and self.db.setting("auto_update"):
                next_check = time.time() + CHECK_EVERY
                try:
                    self.check(install=True)
                except Exception:  # never let a failed update stop the loop
                    log.exception("update check failed")
                finally:
                    self.db.release()
            elif not self.db.setting("auto_update"):
                next_check = time.time() + CHECK_EVERY
