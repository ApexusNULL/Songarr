"""Popular songs by genre and catalogue search, from Deezer's free public API (no key needed).

Spotify removed its recommendation endpoints for personal developer apps, so genre charts
come from Deezer. Results are cached in memory and matched against the library, so the app
can show what's already downloaded and offer "Add" for the rest.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .matching import norm, strip_title

API = "https://api.deezer.com"
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


_net_lock = threading.Lock()
_last_call = 0.0


def _get(path: str, ttl: float) -> dict:
    global _last_call
    with _lock:
        hit = _cache.get(path)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]  # type: ignore[return-value]
    with _net_lock:  # Deezer allows 50 requests per 5 seconds; stay well under it
        time.sleep(max(0.0, _last_call + 0.12 - time.time()))
        _last_call = time.time()
    with urllib.request.urlopen(f"{API}{path}", timeout=20) as r:
        data = json.load(r)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Deezer: {data['error'].get('message', data['error'])}")
    with _lock:
        _cache[path] = (time.time(), data)
    return data


def genres() -> list[dict]:
    data = _get("/genre", 24 * 3600)
    return [{"id": g["id"], "name": g["name"], "image": g.get("picture_medium")}
            for g in data.get("data", []) if g.get("id") is not None]


def _track(t: dict) -> dict:
    album = t.get("album") or {}
    return {
        "deezer_id": t["id"],
        "title": t.get("title") or "",
        "artists": [t.get("artist", {}).get("name")] if t.get("artist") else [],
        "album": album.get("title"),
        "duration_ms": (t.get("duration") or 0) * 1000,
        "explicit": bool(t.get("explicit_lyrics")),
        "cover_url": album.get("cover_xl") or album.get("cover_big"),
        "thumb_url": album.get("cover_medium") or album.get("cover_small"),
        "rank": t.get("rank"),
    }


def genre_chart(genre_id: int, limit: int = 50) -> list[dict]:
    data = _get(f"/chart/{int(genre_id)}/tracks?limit={min(100, int(limit))}", 6 * 3600)
    return [_track(t) for t in data.get("data", [])]


def search(query: str, limit: int = 20) -> list[dict]:
    q = urllib.parse.quote(query)
    data = _get(f"/search/track?q={q}&limit={min(50, int(limit))}", 600)
    return [_track(t) for t in data.get("data", [])]


def _artist(a: dict) -> dict:
    return {"deezer_id": a["id"], "name": a.get("name") or "", "image_url": a.get("picture_xl") or a.get("picture_big"),
            "thumb_url": a.get("picture_medium"), "fans": a.get("nb_fan")}


def chart_artists(limit: int = 15) -> list[dict]:
    data = _get(f"/chart/0/artists?limit={min(100, int(limit))}", 6 * 3600)
    return [_artist(a) for a in data.get("data", []) if a.get("id")]


def find_artist(name: str) -> dict | None:
    """The Deezer artist with this name (exact name match preferred, else the most popular hit)."""
    q = urllib.parse.quote(name)
    hits = _get(f"/search/artist?q={q}&limit=5", 7 * 86400).get("data", [])
    if not hits:
        return None
    want = norm(name)
    best = next((a for a in hits if norm(a.get("name") or "") == want), None)
    if best is None:
        compact = want.replace(" ", "")
        best = next((a for a in hits if norm(a.get("name") or "").replace(" ", "") == compact), None)
    return _artist(best) if best else None


def related_artists(deezer_id: int, limit: int = 12) -> list[dict]:
    data = _get(f"/artist/{int(deezer_id)}/related?limit={min(50, int(limit))}", 7 * 86400)
    return [_artist(a) for a in data.get("data", []) if a.get("id")]


def artist_top(deezer_id: int, limit: int = 10) -> list[dict]:
    data = _get(f"/artist/{int(deezer_id)}/top?limit={min(50, int(limit))}", 24 * 3600)
    return [_track(t) for t in data.get("data", [])]


def full_track(deezer_id: int) -> dict:
    """Everything needed to download and tag a Deezer track (ISRC, track/disc number, release date)."""
    t = _get(f"/track/{int(deezer_id)}", 24 * 3600)
    album = t.get("album") or {}
    artists = [c["name"] for c in t.get("contributors") or [] if c.get("name")] or [t.get("artist", {}).get("name")]
    return {
        "id": f"dz{t['id']}",
        "title": t.get("title") or "",
        "artists": [a for a in artists if a],
        "album": album.get("title"),
        "album_artists": [t.get("artist", {}).get("name")] if t.get("artist") else [],
        "album_id": f"dz{album['id']}" if album.get("id") else None,
        "release_date": t.get("release_date") or album.get("release_date"),
        "track_number": t.get("track_position"),
        "disc_number": t.get("disk_number") or 1,
        "duration_ms": (t.get("duration") or 0) * 1000,
        "isrc": t.get("isrc"),
        "explicit": bool(t.get("explicit_lyrics")),
        "cover_url": album.get("cover_xl") or album.get("cover_big"),
        "thumb_url": album.get("cover_medium"),
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


ALBUM_TRACKS_MAX = 300  # box sets and expanded scores can be long; this keeps one request sane


def _album(a: dict) -> dict:
    return {
        "source": "deezer",
        "id": a["id"],
        "name": a.get("title") or "",
        "artists": [a["artist"]["name"]] if (a.get("artist") or {}).get("name") else [],
        "type": a.get("record_type") or "album",  # album, ep, single, compile
        "count": a.get("nb_tracks"),
        "explicit": bool(a.get("explicit_lyrics")),
        "cover_url": a.get("cover_xl") or a.get("cover_big"),
        "thumb_url": a.get("cover_medium") or a.get("cover_small"),
    }


def artist_releases(deezer_id: int, name: str = "", most: int = 500) -> list[dict]:
    """Everything an artist has released on Deezer (albums, EPs, singles), newest first, each with
    its release date. Deezer lists them in no particular order, so this reads the whole list."""
    out: list[dict] = []
    index = 0
    while index < most:
        data = _get(f"/artist/{int(deezer_id)}/albums?limit=100&index={index}", 3600)
        page = data.get("data", [])
        for a in page:
            if a.get("id"):
                album = _album(a)
                album["artists"] = album["artists"] or ([name] if name else [])
                out.append(album | {"release_date": a.get("release_date") or ""})
        index += len(page)
        if not page or not data.get("next"):
            break
    return sorted(out, key=lambda a: a["release_date"], reverse=True)


def search_albums(query: str, limit: int = 20) -> list[dict]:
    """Albums anywhere (Deezer's catalogue): soundtracks, scores, live albums… Full albums first,
    singles last, otherwise in Deezer's order."""
    q = urllib.parse.quote(query)
    data = _get(f"/search/album?q={q}&limit={min(50, int(limit))}", 600)
    albums = [_album(a) for a in data.get("data", []) if a.get("id")]
    return sorted(albums, key=lambda a: a["type"] == "single")


def album(deezer_id: int) -> dict:
    """An album with every track ready to download and tag (ISRC, track and disc numbers)."""
    a = _get(f"/album/{int(deezer_id)}", 24 * 3600)
    raw: list[dict] = []
    while len(raw) < ALBUM_TRACKS_MAX:
        page = _get(f"/album/{int(deezer_id)}/tracks?index={len(raw)}&limit=100", 24 * 3600)
        data = page.get("data") or []
        raw += data
        if not data or len(raw) >= (page.get("total") or 0):
            break
    out = _album(a)
    album_artists = [c["name"] for c in a.get("contributors") or [] if c.get("name")] or out["artists"]
    release = a.get("release_date")
    added = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tracks = []
    for n, t in enumerate(raw[:ALBUM_TRACKS_MAX], 1):
        who = (t.get("artist") or {}).get("name")
        tracks.append({
            "id": f"dz{t['id']}",
            "deezer_id": t["id"],
            "title": t.get("title") or "",
            "artists": [who] if who else album_artists[:1],
            "album": out["name"],
            "album_artists": album_artists,
            "album_id": f"dz{a['id']}",
            "release_date": release,
            "track_number": t.get("track_position") or n,
            "disc_number": t.get("disk_number") or 1,
            "duration_ms": (t.get("duration") or 0) * 1000,
            "isrc": t.get("isrc"),
            "explicit": bool(t.get("explicit_lyrics")),
            "cover_url": out["cover_url"],
            "thumb_url": out["thumb_url"],
            "added_at": added,
        })
    genres = [g["name"] for g in (a.get("genres") or {}).get("data", []) if g.get("name")]
    return out | {"artists": album_artists, "release_date": release, "label": a.get("label"), "genres": genres,
                  "count": len(tracks), "tracks": tracks}


def preview_url(deezer_id: int | None = None, isrc: str | None = None, artist: str = "", title: str = "") -> str | None:
    """A fresh link to Deezer's 30-second preview of a song (the links expire, so they aren't stored).
    Found by Deezer id, else by ISRC, else by searching artist and title."""
    if deezer_id:
        return _get(f"/track/{int(deezer_id)}", 1800).get("preview") or None
    if isrc and re.fullmatch(r"[A-Za-z0-9]{12}", isrc):
        try:
            return _get(f"/track/isrc:{isrc.upper()}", 1800).get("preview") or None
        except RuntimeError:
            pass  # unknown ISRC: fall back to searching
    if title:
        q = urllib.parse.quote(f"{artist} {strip_title(title)}".strip())
        hits = [t for t in _get(f"/search/track?q={q}&limit=10", 1800).get("data", []) if t.get("preview")]
        want, who = norm(strip_title(title)), norm(artist)
        same_title = [t for t in hits if norm(strip_title(t.get("title") or "")) == want]
        for t in same_title:
            if not who or norm((t.get("artist") or {}).get("name") or "") == who:
                return t["preview"]
        return (same_title or hits or [{}])[0].get("preview")
    return None


def song_key(artists: list[str], title: str) -> str:
    """Loose identity used to spot songs already in the library: first artist + cleaned title."""
    return f"{norm(artists[0] if artists else '')}|{norm(strip_title(title))}"
