"""Shared fixtures: tiny real audio files and a fake YouTube backend."""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from songarr import artist_info, podcast_downloads, recommend
from songarr.matching import Candidate, TrackInfo, score
from songarr.youtube import Blocked, NeedsSignIn, Unavailable, find_ffmpeg

FFMPEG = find_ffmpeg()
recommend.STARTUP_DELAY = 10**6  # tests never reach Deezer or Apple; they call Recommender.refresh directly
podcast_downloads.STARTUP_DELAY = 10**6  # likewise PodcastDownloads.cycle
artist_info.STARTUP_DELAY = 10**6  # no background bio prefetching in tests
# Offline: anything a test doesn't fake goes to a closed local port and fails at once.
from songarr import dependencies, discover, lyrics, podcasts  # noqa: E402
_NOWHERE = "http://127.0.0.1:9"
discover.API = artist_info.WIKI = artist_info.WIKIDATA = artist_info.LASTFM = podcasts.ITUNES = lyrics.API = _NOWHERE
dependencies.PYPI = _NOWHERE  # and never PyPI
_CACHE = Path(tempfile.gettempdir()) / "songarr-test-audio"


def audio_file(ext: str) -> Path:
    """A 1-second sine tone in the given container, generated once with FFmpeg."""
    _CACHE.mkdir(exist_ok=True)
    p = _CACHE / f"tone.{ext}"
    if not p.exists():
        codec = {"m4a": ["-c:a", "aac"], "mp3": ["-c:a", "libmp3lame"], "opus": ["-c:a", "libopus"]}[ext]
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", *codec, str(p)],
                       check=True)
    return p


def spotify_track(n: int, **kw) -> dict:
    t = {
        "id": f"{n:022d}", "title": f"Song {n}", "artists": [f"Artist {n % 3}"], "album": f"Album {n % 2}",
        "album_artists": [f"Artist {n % 3}"], "album_id": f"al{n % 2}", "release_date": "2020-01-02",
        "track_number": n, "disc_number": 1, "duration_ms": 180_000, "isrc": None, "explicit": False,
        "cover_url": None, "thumb_url": None, "added_at": f"2026-01-01T00:00:{n:02d}Z",
    }
    t.update(kw)
    return t


class FakeYouTube:
    """Stands in for songarr.youtube.YouTube. Behaviour is chosen by words in the song title:
    'obscure' -> only a weak match, 'missing' -> nothing found, 'bot' -> bot check,
    'forbidden' -> HTTP 403 on download, 'broken' -> download raises Unavailable."""

    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self.pace = lambda kind: None  # the service installs its pacer here
        self.paced: list[tuple[str, float]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.downloads: list[str] = []

    def _cand(self, track: TrackInfo, vid: str, title: str, artist: bool = True) -> Candidate:
        c = Candidate(id=vid, title=title, duration=track.duration, channel=f"{track.artists[0]} - Topic" if artist else "Someone",
                      artists=track.artists if artist else ["Someone"], track=title, source="ytm")
        return score(track, c)

    def find(self, track: TrackInfo, threshold: float, on_stage=lambda s: None):
        self.pace("search")
        with self.lock:
            self.paced.append(("search", time.time()))
        on_stage("searching")
        if "missing" in track.title:
            return None, []
        if "bot" in track.title:
            raise Blocked("bot", "Sign in to confirm you're not a bot")
        if "forbidden" in track.title:
            raise Blocked("throttle", "unable to download video data: HTTP Error 403: Forbidden")
        if "obscure" in track.title:
            c = self._cand(track, "weakmatch01", "Totally Different Tune", artist=False)
            return c, [c]
        prefix = "age" if "agegate" in track.title else "v"
        good = self._cand(track, f"{prefix}{abs(hash(track.title)) % 10**8:08d}", track.title)
        alt = self._cand(track, "alt00000001", track.title + " (Live)")
        return good, sorted([good, alt], key=lambda c: c.score, reverse=True)

    def details(self, vid: str, source: str = "yt") -> Candidate:
        return Candidate(id=vid, title="Picked by hand", duration=180, channel="X", source=source)

    def download(self, vid: str, workdir: Path, fmt: str, progress):
        self.pace("download")
        with self.lock:
            self.paced.append(("download", time.time()))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            for pct in (0.25, 0.5, 1.0):
                progress({"status": "downloading", "downloaded_bytes": int(pct * 1000), "total_bytes": 1000, "speed": 5e5})
                time.sleep(self.delay / 3)
            if vid == "broken0000x":
                raise Unavailable("Video unavailable")
            if vid.startswith("age"):
                raise NeedsSignIn("Sign in to confirm your age")
            workdir.mkdir(parents=True, exist_ok=True)
            out = workdir / f"{vid}.{fmt}"
            out.write_bytes(audio_file(fmt).read_bytes())
            progress({"status": "finished"})
            with self.lock:
                self.downloads.append(vid)
            return out, {"abr": 129.6}
        finally:
            with self.lock:
                self.active -= 1
