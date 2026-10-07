"""Songarr keeps itself up to date from its git repository.

When Songarr runs from a git clone (as the setup guide and installer/install-linux.sh set it up), each
update check (dependencies.py: every few hours, and "Check for updates now") also asks the clone's
`origin` for new commits on the branch it follows. New ones are fast-forwarded to, never merged or
forced: if the folder has changes of its own, or commits the repository doesn't have, Songarr only
says an update is waiting. If requirements.txt changed, it's installed again. The new code has to
import before Songarr restarts into it (once nobody's listening); if it doesn't, the previous commit is
put back and that commit isn't tried again. A copy that isn't a git clone (the Windows installer's)
is updated with a newer installer instead.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT = 120
MODULES = ["songarr.__main__", "songarr.web", "songarr.appapi", "songarr.service"]  # must import after an update


def _python() -> str:
    """python.exe beside pythonw.exe (as in dependencies.py)."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)


def head_commit(root: Path = ROOT) -> str | None:
    """The commit a clone is on, read straight from .git (no git needed), or None."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        if (git / ref).is_file():
            return (git / ref).read_text(encoding="utf-8").strip() or None
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


RUNNING = head_commit()  # the code this process started with (a pull doesn't change it until a restart)


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


class SelfUpdate:
    def __init__(self, root: Path | None = None, modules: list[str] | None = None):
        self.root = Path(root) if root else ROOT  # (tests point ROOT elsewhere, so they never fetch)
        self.modules = modules or MODULES
        self.state: dict = {"checked": None, "behind": 0, "upstream": None, "blocked": None, "error": None}

    # -- git ------------------------------------------------------------------------------------------

    def git(self, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[bool, str]:
        exe = shutil.which("git")
        if exe is None:
            return False, "git isn't installed"
        try:
            r = subprocess.run([exe, *args], cwd=self.root, capture_output=True, text=True, timeout=timeout,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                               env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})  # never wait for a password
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def is_clone(self) -> bool:
        return (self.root / ".git").exists() and shutil.which("git") is not None

    def _out(self, *args: str) -> str | None:
        ok, out = self.git(*args)
        return out.splitlines()[-1].strip() if ok and out else None

    def remote(self) -> str | None:
        url = self._out("remote", "get-url", "origin")
        return re.sub(r"//[^/@]+@", "//", url) if url else None  # never show a token in a URL

    # -- what's there ----------------------------------------------------------------------------------

    def info(self) -> dict:
        if not self.is_clone():
            return {"git": False, "running": RUNNING[:7] if RUNNING else None}
        head = head_commit(self.root)
        when = self._out("log", "-1", "--format=%cI")
        return {"git": True, "running": RUNNING[:7] if RUNNING else None, "commit": head[:7] if head else None,
                "date": when, "branch": self._out("rev-parse", "--abbrev-ref", "HEAD"), "remote": self.remote()} | self.state

    def check(self, failed: str | None = None) -> dict:
        """Ask origin for new commits. [failed]: a commit that didn't work before (not tried again)."""
        self.state.update(checked=time.time(), error=None, blocked=None, behind=0, upstream=None)
        if not self.is_clone():
            return self.state
        ok, out = self.git("fetch", "--quiet", "origin")
        if not ok:
            self.state["error"] = f"Couldn't reach the repository: {out[-200:]}"
            return self.state
        upstream = self._out("rev-parse", "@{upstream}")
        if not upstream:
            self.state["error"] = "This clone's branch doesn't follow one on origin (git branch -u origin/main)."
            return self.state
        behind = int(self._out("rev-list", "--count", "HEAD..@{upstream}") or 0)
        ahead = int(self._out("rev-list", "--count", "@{upstream}..HEAD") or 0)
        self.state.update(behind=behind, upstream=upstream[:7])
        if behind:
            if self._out("status", "--porcelain", "--untracked-files=no"):
                self.state["blocked"] = "This folder has changes of its own, so it isn't updated by itself."
            elif ahead:
                self.state["blocked"] = "This folder has commits the repository doesn't have, so it isn't updated by itself."
            elif failed and upstream.startswith(failed):
                self.state["blocked"] = "The newest version didn't start here, so the one that works is kept until the next."
        return self.state

    # -- updating -------------------------------------------------------------------------------------

    def update(self, pip_install: Callable[[list[str]], tuple[bool, str]]) -> dict:
        """Fast-forward to origin (after check() found it behind and not blocked). Returns
        {"updated", "from", "to", "count", "subject", "error"}."""
        old = head_commit(self.root) or ""
        requirements = self.root / "requirements.txt"
        before = _hash(requirements)
        ok, out = self.git("merge", "--ff-only", "@{upstream}")
        new = head_commit(self.root) or ""
        if not ok:
            return {"updated": False, "error": f"Couldn't update: {out[-200:]}"}
        result = {"updated": True, "from": old[:7], "to": new[:7], "count": self.state.get("behind") or 0,
                  "subject": self._out("log", "-1", "--format=%s"), "error": None}
        if _hash(requirements) != before:
            ok, out = pip_install(["-r", str(requirements)])
            if not ok:
                return self._back(old, result, f"its new requirements didn't install: {out[-200:]}")
        ok, out = self._imports()
        if not ok:
            return self._back(old, result, f"the new code didn't start: {out[-300:]}")
        self.state.update(behind=0, blocked=None)
        return result

    def _imports(self) -> tuple[bool, str]:
        try:
            r = subprocess.run([_python(), "-c", "import " + ", ".join(self.modules)], cwd=self.root, capture_output=True,
                               text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def _back(self, old: str, result: dict, why: str) -> dict:
        self.git("reset", "--hard", old)  # no changes of its own (check() made sure), so nothing else is lost
        self.state["blocked"] = "The newest version didn't start here, so the one that works is kept until the next."
        return result | {"updated": False, "failed": result["to"], "error": f"Kept {old[:7]}: {why}"}
