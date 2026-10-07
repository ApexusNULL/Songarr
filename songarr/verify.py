"""Sign in to YouTube once, in a real browser window, so downloads run as a signed-in user.

YouTube's "Sign in to confirm you're not a bot" wants exactly that: a signed-in session.
Chrome locks its own cookies against other programs, so instead of reading them, Songarr
opens a separate Chrome window with its own profile and DevTools port. You sign in there
yourself (and complete any check YouTube shows); Chrome then hands over that window's YouTube
cookies through the DevTools protocol, Songarr saves them for yt-dlp and closes the window.
The profile is kept, so signing in again later is usually a single click.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

log = logging.getLogger(__name__)

YOUTUBE_URL = "https://music.youtube.com/"
SIGNED_IN_COOKIES = {"LOGIN_INFO", "SAPISID", "__Secure-3PAPISID", "SID"}


def find_chrome() -> str | None:
    for p in (
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if Path(p).exists():
            return p
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def netscape_cookies(cookies: list[dict]) -> str:
    """CDP cookies -> the Netscape cookies.txt format yt-dlp reads."""
    lines = ["# Netscape HTTP Cookie File", "# Written by Songarr from its YouTube sign-in window. Keep private."]
    for c in cookies:
        domain = c["domain"]
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c["expires"])) if c.get("expires", -1) > 0 else "0",
            c["name"],
            c.get("value", ""),
        ]))
    return "\n".join(lines) + "\n"


class YouTubeSignIn:
    def __init__(self, data_dir: Path):
        self.profile = Path(data_dir) / "youtube-browser"
        self.cookies_path = Path(data_dir) / "youtube-cookies.txt"
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self._lock = threading.Lock()

    @property
    def window_open(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open_window(self) -> None:
        chrome = find_chrome()
        if not chrome:
            raise RuntimeError("Chrome (or Edge) wasn't found on this PC.")
        with self._lock:
            if self.window_open:
                return
            self.profile.mkdir(parents=True, exist_ok=True)
            self.port = _free_port()
            # A separate --user-data-dir is required: Chrome refuses remote debugging on your normal profile.
            self.proc = subprocess.Popen([
                chrome, f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=http://127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--new-window", YOUTUBE_URL,
            ])

    def _cdp(self, method: str, params: dict | None = None):
        deadline = time.time() + 15
        while True:  # Chrome needs a moment after launch before DevTools answers
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=3) as r:
                    ws_url = json.load(r)["webSocketDebuggerUrl"]
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("Couldn't talk to the sign-in window. Close it and try again.") from None
                time.sleep(0.5)
        with connect(ws_url, max_size=None, open_timeout=10, origin="http://127.0.0.1") as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv(timeout=15))
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(f"Chrome said: {msg['error'].get('message')}")
                    return msg.get("result") or {}

    def youtube_cookies(self) -> list[dict]:
        cookies = self._cdp("Storage.getCookies").get("cookies") or []
        return [c for c in cookies if c.get("domain", "").lstrip(".").endswith("youtube.com")]

    def signed_in(self) -> bool:
        return self.window_open and bool(SIGNED_IN_COOKIES & {c["name"] for c in self.youtube_cookies()})

    def finish(self) -> dict:
        """Save the window's YouTube session for yt-dlp, then close the window."""
        if not self.window_open:
            raise RuntimeError("The sign-in window isn't open. Click 'Sign in to YouTube' first.")
        cookies = self.youtube_cookies()
        names = {c["name"] for c in cookies}
        if not SIGNED_IN_COOKIES & names:
            raise RuntimeError("That window isn't signed in to YouTube yet. Sign in there, then click Done.")
        tmp = self.cookies_path.with_suffix(".partial")
        tmp.write_text(netscape_cookies(cookies), encoding="utf-8")
        os.replace(tmp, self.cookies_path)
        self.close_window()
        return {"cookies": len(cookies), "path": str(self.cookies_path)}

    def close_window(self) -> None:
        with self._lock:
            if self.window_open:
                try:
                    self._cdp("Browser.close")
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def status(self) -> dict:
        info = {"window_open": self.window_open, "saved": self.cookies_path.exists(), "saved_at": None}
        if info["saved"]:
            info["saved_at"] = self.cookies_path.stat().st_mtime
        return info
