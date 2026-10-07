"""Song lyrics from LRCLIB (lrclib.net), a free, open lyrics database.

Synced lyrics (lines with timestamps) when LRCLIB has them, otherwise plain text. A song is
matched by artist, title, album and length; if that finds nothing, by searching artist and
title and keeping a result within a few seconds of the song's length. Lyrics are cached
(misses for a week, so new uploads to LRCLIB are picked up).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .db import DB
from .matching import norm, strip_title

log = logging.getLogger(__name__)

API = "https://lrclib.net/api"
UA = "Songarr/0.1 (self-hosted personal music server)"
KEEP = 180 * 86400
KEEP_MISS = 7 * 86400
_STAMP = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")

_lock = threading.Lock()
_last = 0.0


def _get(path: str, **params: Any) -> Any:
    """GET an LRCLIB endpoint; None for 404."""
    global _last
    with _lock:  # one request at a time, a little apart
        time.sleep(max(0.0, _last + 0.25 - time.time()))
        _last = time.time()
    url = f"{API}/{path}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
            return json.loads(r.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def parse_lrc(text: str) -> list[dict]:
    """'[01:02.50] line' (several stamps per line allowed) -> [{"t": ms, "text": line}], in time order."""
    out = []
    for raw in (text or "").splitlines():
        stamps = _STAMP.findall(raw)
        if not stamps:
            continue
        words = _STAMP.sub("", raw).strip()
        for m, s in stamps:
            secs = float(s.replace(":", "."))
            out.append({"t": int((int(m) * 60 + secs) * 1000), "text": words})
    out.sort(key=lambda x: x["t"])
    return out


def _pick(results: list[dict], artist: str, title: str, seconds: int | None) -> dict | None:
    want_artist, want_title = norm(artist), norm(strip_title(title))
    good = []
    for r in results or []:
        if norm(strip_title(r.get("trackName") or "")) != want_title:
            continue
        if want_artist and want_artist not in norm(r.get("artistName") or ""):
            continue
        if seconds and r.get("duration") and abs(r["duration"] - seconds) > 3:
            continue
        good.append(r)
    good.sort(key=lambda r: (not r.get("syncedLyrics"), not r.get("plainLyrics") and not r.get("instrumental")))
    return good[0] if good else None


def fetch(artist: str, title: str, album: str | None, seconds: int | None) -> dict | None:
    hit = None
    if artist and title and album and seconds:
        hit = _get("get", artist_name=artist, track_name=title, album_name=album, duration=seconds)
    if hit is None:
        hit = _pick(_get("search", artist_name=artist, track_name=strip_title(title)) or [], artist, title, seconds)
    if hit is None or not (hit.get("syncedLyrics") or hit.get("plainLyrics") or hit.get("instrumental")):
        return None
    synced = parse_lrc(hit.get("syncedLyrics") or "")
    return {
        "synced": synced or None,
        "plain": (hit.get("plainLyrics") or "").strip() or ("\n".join(x["text"] for x in synced) if synced else None),
        "instrumental": bool(hit.get("instrumental")),
        "source": "LRCLIB",
    }


class Lyrics:
    def __init__(self, db: DB):
        self.db = db

    def get(self, track: dict) -> dict | None:
        """Lyrics for a track row (cached); None when there are none to be found."""
        row = self.db.one("SELECT data, fetched FROM lyrics WHERE track_id = ?", (track["id"],))
        if row is not None:
            data = json.loads(row["data"])
            if time.time() - row["fetched"] < (KEEP if data else KEEP_MISS):
                return data
        artists = json.loads(track["artists"]) if isinstance(track["artists"], str) else track["artists"]
        seconds = round((track["duration_ms"] or 0) / 1000) or None
        try:
            data = fetch(artists[0] if artists else "", track["title"], track["album"], seconds)
        except (OSError, ValueError) as e:
            log.info("lyrics for %s unavailable: %s", track["id"], e)
            return json.loads(row["data"]) if row is not None else None  # don't cache a failed lookup
        self.db.run("INSERT OR REPLACE INTO lyrics(track_id, data, fetched) VALUES (?,?,?)",
                    (track["id"], json.dumps(data), time.time()))
        return data
