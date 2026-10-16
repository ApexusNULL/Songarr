"""Song lyrics from LRCLIB (lrclib.net), a free, open lyrics database.

Synced lyrics (lines with timestamps) when LRCLIB has them, otherwise plain text. A song is
matched by artist, title, album and length; if that finds nothing, by searching artist and
title, then by a plain search (an artist spelt otherwise there), keeping a result with the same
title, one of the song's artists and the same length (within a few seconds for words timed to the
music; plain words may be further off). Lyrics are cached (misses for a week, so new uploads to
LRCLIB are picked up).

When LRCLIB is busy (it asked to slow down, or isn't answering), that's [Busy], not "no lyrics":
nothing is cached, and after a "slow down" it isn't asked again for as long as it said.
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
SPACING = 0.6  # seconds between requests
SYNCED_WITHIN = 5  # seconds: words timed to the music only for the same recording
PLAIN_WITHIN = 20  # plain words can come from a slightly different one (a radio edit, a remaster)
_STAMP = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")

_lock = threading.Lock()
_last = 0.0
_quiet_until = 0.0  # LRCLIB asked to slow down: not asked again before this


class Busy(Exception):
    """LRCLIB is busy (it asked to slow down) or isn't answering: try again later."""


def _get(path: str, **params: Any) -> Any:
    """GET an LRCLIB endpoint; None for nothing found. One more try when it stumbles."""
    global _last, _quiet_until
    url = f"{API}/{path}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    for attempt in (1, 2):
        with _lock:  # one request at a time, a little apart
            if time.time() < _quiet_until:
                raise Busy(f"LRCLIB asked to wait {_quiet_until - time.time():.0f} more seconds")
            time.sleep(max(0.0, _last + SPACING - time.time()))
            _last = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as r:
                return json.loads(r.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (400, 404):  # nothing for that question
                return None
            if e.code == 429:
                wait = _seconds(e.headers.get("Retry-After"), 60)
                with _lock:
                    _quiet_until = time.time() + wait
                raise Busy(f"LRCLIB asked to slow down for {wait:.0f} seconds") from e
            if attempt == 2:
                raise Busy(f"LRCLIB isn't answering properly (HTTP {e.code})") from e
        except (OSError, ValueError) as e:  # away, timed out, or half an answer
            if attempt == 2:
                raise Busy(f"LRCLIB can't be reached ({e})") from e
        time.sleep(1.5)
    return None


def _seconds(value: str | None, default: float) -> float:
    try:
        return min(max(float(value or default), 1.0), 600.0)
    except ValueError:
        return default


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


def _pick(results: list[dict], artists: list[str], title: str, seconds: int | None) -> dict | None:
    """The best of LRCLIB's [results] for the song: the same title, one of its [artists] (either name
    in the other: "Queen" / "Queen & David Bowie"), and the same length: within SYNCED_WITHIN
    seconds for words timed to the music, PLAIN_WITHIN for plain words (the timing is dropped then)."""
    want_title = norm(strip_title(title))
    wanted = [n for a in artists if (n := norm(a))]
    best = None
    for r in results or []:
        if norm(strip_title(r.get("trackName") or "")) != want_title:
            continue
        got = norm(r.get("artistName") or "")
        if wanted and not (got and any(a in got or got in a for a in wanted)):
            continue
        off = abs(r["duration"] - seconds) if seconds and r.get("duration") else 0
        timed = bool(r.get("syncedLyrics")) and off <= SYNCED_WITHIN
        if not timed and (off > PLAIN_WITHIN or not (r.get("plainLyrics") or r.get("syncedLyrics") or r.get("instrumental"))):
            continue
        rank = (timed, bool(r.get("plainLyrics") or r.get("syncedLyrics")), -off)
        if best is None or rank > best[0]:
            best = (rank, r)
    if best is None:
        return None
    hit = dict(best[1])
    if not best[0][0] and hit.get("syncedLyrics"):  # another recording's timing: the words only
        hit["plainLyrics"] = hit.get("plainLyrics") or "\n".join(x["text"] for x in parse_lrc(hit["syncedLyrics"]))
        hit["syncedLyrics"] = None
    return hit


def fetch(artist: str, title: str, album: str | None, seconds: int | None, artists: list[str] | None = None) -> dict | None:
    names = [a for a in (artists or [artist]) if a]
    hit = None
    if artist and title and album and seconds:
        hit = _get("get", artist_name=artist, track_name=title, album_name=album, duration=seconds)
    if hit is None:
        hit = _pick(_get("search", artist_name=artist, track_name=strip_title(title)) or [], names, title, seconds)
    if hit is None:  # the artist (or the title) written otherwise there: a plain search
        hit = _pick(_get("search", q=f"{artist} {strip_title(title)}".strip()) or [], names, title, seconds)
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
        """Lyrics for a track row (cached); None when there are none to be found. Raises [Busy] when
        LRCLIB can't say just now."""
        row = self.db.one("SELECT data, fetched FROM lyrics WHERE track_id = ?", (track["id"],))
        if row is not None:
            data = json.loads(row["data"])
            if time.time() - row["fetched"] < (KEEP if data else KEEP_MISS):
                return data
        artists = json.loads(track["artists"]) if isinstance(track["artists"], str) else track["artists"]
        seconds = round((track["duration_ms"] or 0) / 1000) or None
        try:
            data = fetch(artists[0] if artists else "", track["title"], track["album"], seconds, artists)
        except (Busy, OSError, ValueError) as e:
            log.info("lyrics for %s unavailable for now: %s", track["id"], e)
            if row is not None:
                return json.loads(row["data"])  # what was found before (not cached again: tried later)
            raise Busy(str(e)) from e
        self.db.run("INSERT OR REPLACE INTO lyrics(track_id, data, fetched) VALUES (?,?,?)",
                    (track["id"], json.dumps(data), time.time()))
        return data
