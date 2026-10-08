"""YouTube search (YouTube Music's own search API) and audio download (yt-dlp).

Request budget per song, which is what keeps YouTube's bot detection quiet:
  1 YouTube Music search (title, artists, album, length, official-audio flag in one call)
  (+1 ISRC search and +1 plain YouTube search only when nothing matched well)
  1 yt-dlp extraction + the audio stream for the download itself
Every request first goes through `pace(kind)`, which the service uses to space requests
out across all workers and to hold everything during a cooldown.

Chrome on Windows (127+) locks its cookies to the Chrome process, so yt-dlp cannot read
them directly; signing in is optional via an exported cookies.txt.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadError
from ytmusicapi import YTMusic

from .matching import Candidate, TrackInfo, score

log = logging.getLogger(__name__)

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|^)([A-Za-z0-9_-]{11})(?:[?&#/]|$)")


def youtube_id(text: str) -> str | None:
    """The video id in a YouTube link (watch, youtu.be, shorts, music.youtube.com...) or a bare id."""
    m = _YT_ID.search(text.strip())
    return m[1] if m else None

FORMATS = {
    # m4a: YouTube's AAC as-is (itag 141 at 256k with a Premium cookie, else 140 at 128k)
    "m4a": ("bestaudio[ext=m4a]/bestaudio/best", "m4a"),
    # opus: YouTube's Opus as-is (itag 251)
    "opus": ("bestaudio[acodec=opus]/bestaudio/best", "opus"),
    # mp3: re-encoded (VBR V0) from the best source
    "mp3": ("bestaudio/best", "mp3"),
}


class Blocked(Exception):
    """YouTube is throttling this connection.

    kind 'bot'      -> "Sign in to confirm you're not a bot" or HTTP 429: long cooldown
    kind 'throttle' -> HTTP 403 on the media stream or degraded pages: shorter cooldown
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class Unavailable(Exception):
    pass


class NeedsSignIn(Exception):
    """This upload only plays for signed-in users (age-restricted or sign-in-only)."""


def _winget(exe: str, package: str) -> str | None:
    """A program winget installed, for just this user or for everyone, that isn't on this process's
    PATH yet (Songarr started by the installer that just installed it, say)."""
    for base in (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet"), os.path.expandvars(r"%ProgramFiles%\WinGet")):
        link = Path(base) / "Links" / exe
        if link.exists():
            return str(link)
        hits = sorted(glob.glob(os.path.join(base, "Packages", f"*{package}*", "**", exe), recursive=True))
        if hits:
            return hits[-1]
    return None


def find_ffmpeg(configured: str = "") -> str | None:
    if configured and Path(configured).exists():
        return configured
    if exe := shutil.which("ffmpeg"):
        return exe
    return _winget("ffmpeg.exe", "FFmpeg")


def find_js_runtime() -> dict:
    """yt-dlp needs a JavaScript runtime for YouTube; it defaults to Deno, so name what exists."""
    for name in ("deno", "node", "bun"):
        if path := shutil.which(name):
            return {name: {"path": path}}
    if path := _winget("deno.exe", "Deno"):
        return {"deno": {"path": path}}
    node = Path(os.path.expandvars(r"%ProgramFiles%\nodejs\node.exe"))
    return {"node": {"path": str(node)}} if node.exists() else {}


class _Log:
    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def classify(e: Exception) -> Exception:
    msg = str(e).lower()
    if ("confirm you" in msg and "bot" in msg) or "http error 429" in msg or "too many requests" in msg:
        return Blocked("bot", str(e))
    if "http error 403" in msg or "unable to extract yt initial data" in msg:
        return Blocked("throttle", str(e))
    if "confirm your age" in msg or "please sign in" in msg or "age-restricted" in msg or "inappropriate for some users" in msg:
        return NeedsSignIn(str(e))
    if any(k in msg for k in ("video unavailable", "this video is unavailable", "private video", "has been removed",
                              "not available", "members-only", "copyright")):
        return Unavailable(str(e))
    return e


class YouTube:
    def __init__(self, ffmpeg: str | None = None, cookies_file: str = "", js_runtimes: dict | None = None,
                 pace: Callable[[str], None] | None = None):
        self.ffmpeg = ffmpeg
        self.cookies_file = cookies_file if cookies_file and Path(cookies_file).exists() else ""
        self.js_runtimes = js_runtimes if js_runtimes is not None else find_js_runtime()
        self.pace: Callable[[str], None] = pace or (lambda kind: None)
        self._local = threading.local()

    def _opts(self, **extra) -> dict:
        o = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _Log(),
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "noplaylist": True,
            "js_runtimes": self.js_runtimes,
            "sleep_interval_requests": 0.75,  # yt-dlp's own "-t sleep" spacing between page/API requests
        }
        if self.cookies_file:
            o["cookiefile"] = self.cookies_file
        if self.ffmpeg:
            o["ffmpeg_location"] = self.ffmpeg
        o.update(extra)
        return o

    @property
    def ytm(self) -> YTMusic:
        if getattr(self._local, "ytm", None) is None:  # its HTTP session is not thread-safe
            self._local.ytm = YTMusic()
        return self._local.ytm

    # -- search -------------------------------------------------------------

    def search_music(self, query: str, n: int = 5, source: str = "ytm") -> list[Candidate]:
        """YouTube Music 'songs' search: one request, full metadata for every result."""
        self.pace("search")
        try:
            results = self.ytm.search(query, filter="songs", limit=n)
        except Exception as e:  # ytmusicapi raises plain exceptions carrying the HTTP status
            if "429" in str(e):
                raise Blocked("bot", f"YouTube Music search rate-limited: {e}") from None
            log.info("YouTube Music search failed for %r: %s", query, e)
            return []
        out = []
        for r in results or []:
            vid = r.get("videoId") or ""
            if r.get("resultType") not in ("song", None) or not VIDEO_ID.fullmatch(vid):
                continue  # mixes, playlists, albums
            artists = [a["name"] for a in r.get("artists") or [] if a.get("name")]
            out.append(Candidate(
                id=vid, title=r.get("title") or "", duration=r.get("duration_seconds"),
                channel=", ".join(artists) or None, artists=artists, track=r.get("title"),
                album=(r.get("album") or {}).get("name"), source=source,
            ))
        return out[:n]

    def search_videos(self, query: str, n: int = 6) -> list[Candidate]:
        """Plain YouTube search (flat: one request, includes length and channel)."""
        self.pace("search")
        try:
            with yt_dlp.YoutubeDL(self._opts(extract_flat="in_playlist", playlistend=n)) as y:
                info = y.extract_info(f"ytsearch{n}:{query}", download=False)
        except DownloadError as e:
            raise classify(e) from None
        out = []
        for e in (info or {}).get("entries") or []:
            vid = (e or {}).get("id") or ""
            url = e.get("url") or ""
            if e.get("ie_key") not in (None, "Youtube") or not VIDEO_ID.fullmatch(vid) or "/shorts/" in url:
                continue
            out.append(Candidate(id=vid, title=e.get("title") or "", duration=e.get("duration"),
                                 channel=e.get("channel") or e.get("uploader"), source="yt"))
        return out[:n]

    def details(self, video_id: str, source: str = "yt") -> Candidate:
        """Full metadata for one upload (used for hand-picked links only)."""
        self.pace("search")
        try:
            with yt_dlp.YoutubeDL(self._opts()) as y:
                i = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
        except DownloadError as e:
            raise classify(e) from None
        return Candidate(
            id=video_id, title=i.get("title") or "", duration=i.get("duration"),
            channel=i.get("channel") or i.get("uploader"), artists=list(i.get("artists") or []),
            track=i.get("track"), album=i.get("album"), source=source,
        )

    def find(self, track: TrackInfo, threshold: float, on_stage: Callable[[str], None] = lambda s: None,
             ) -> tuple[Candidate | None, list[Candidate]]:
        """Best upload for `track` plus every candidate scored on the way.

        YouTube Music by artist + title first (official label audio, usually the only request
        needed), then by ISRC, then plain YouTube; stops as soon as something clears `threshold`.
        """
        seen: dict[str, Candidate] = {}

        def add(cands: list[Candidate]) -> Candidate | None:
            for c in cands:
                if c.id not in seen:
                    seen[c.id] = score(track, c)
                elif c.source == "isrc" and seen[c.id].source != "isrc":  # the same upload, now found by its ISRC
                    again = score(track, c)
                    if again.score > seen[c.id].score:
                        seen[c.id] = again
            return max(seen.values(), key=lambda c: c.score, default=None)

        on_stage("searching YouTube Music")
        best = add(self.search_music(track.query, 5, "ytm"))
        if (best is None or best.score < threshold) and track.isrc:
            best = add(self.search_music(track.isrc, 3, "isrc"))
        if best is None or best.score < threshold:
            on_stage("searching YouTube")
            add(self.search_videos(track.query, 6))
        ranked = sorted(seen.values(), key=lambda c: c.score, reverse=True)
        return (ranked[0] if ranked else None), ranked

    # -- download -----------------------------------------------------------

    def download(self, video_id: str, workdir: Path, fmt: str, progress: Callable[[dict], None]) -> tuple[Path, dict]:
        self.pace("download")
        selector, codec = FORMATS.get(fmt, FORMATS["m4a"])
        workdir.mkdir(parents=True, exist_ok=True)
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if codec == "mp3":
            pp["preferredquality"] = "0"
        opts = self._opts(
            format=selector,
            outtmpl=str(workdir / f"{video_id}.%(ext)s"),
            progress_hooks=[progress],
            postprocessors=[pp],
            writethumbnail=False,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        except DownloadError as e:
            raise classify(e) from None
        files = [Path(d["filepath"]) for d in info.get("requested_downloads") or [] if d.get("filepath")]
        files = [f for f in files if f.exists()] or sorted(workdir.glob(f"{video_id}.{codec}"))
        if not files:
            raise RuntimeError(f"yt-dlp finished but no .{codec} file appeared in {workdir}")
        return files[0], info
