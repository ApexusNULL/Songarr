"""Bring someone's Spotify library over from Exportify (https://exportify.app) without asking Spotify:
they export it themselves (Exportify uses its own Spotify app, so Songarr's limit doesn't come into
it) and the admin page imports the file into their profile.

A .csv is one playlist; the .zip from "Export All" holds one .csv per playlist. Liked Songs (Exportify
calls them "Liked", so liked.csv) become the person's likes; every other file becomes one of their own
playlists. Each row already carries what Songarr needs (Spotify's id,
title, artists, album, length, ISRC, cover art), so nothing is looked up anywhere.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import time
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service

MAX_UPLOAD = 50 * 1024 * 1024
# Exportify's English column names -> ours (it names columns in the browser's language)
COLUMNS = {
    "track uri": "uri", "track name": "title", "artist name(s)": "artists", "album uri": "album_uri",
    "album name": "album", "album artist name(s)": "album_artists", "album release date": "release_date",
    "album image url": "cover_url", "disc number": "disc_number", "track number": "track_number",
    "track duration (ms)": "duration_ms", "explicit": "explicit", "isrc": "isrc", "added at": "added_at",
}
_TRACK_URI = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
_ALBUM_URI = re.compile(r"spotify:album:([A-Za-z0-9]{22})")


class ExportifyError(Exception):
    pass


def split_names(value: str) -> list[str]:
    """Exportify joins several artists with ", " and writes a comma inside a name as "\\,"."""
    names = re.split(r"(?<!\\), ", value or "")
    return [n.replace("\\,", ",").strip() for n in names if n.strip()]


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _thumb(cover: str | None) -> str | None:
    # Spotify's covers: 640 px ...b273, 300 px ...1e02, 64 px ...4851 (same picture, same path otherwise)
    return cover.replace("ab67616d0000b273", "ab67616d00004851") if cover else None


def read_csv(text: str) -> list[dict]:
    """One Exportify .csv -> track records (as spotify.parse_saved_item makes them), file order.
    Local files and episodes (no Spotify track id) are left out."""
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(rows, None)
    if not header:
        raise ExportifyError("That file is empty.")
    where = {COLUMNS[h.strip().lower()]: i for i, h in enumerate(header) if h.strip().lower() in COLUMNS}
    if not {"uri", "title", "artists"} <= where.keys():
        raise ExportifyError("That doesn't look like an Exportify export (there's no Track URI column). "
                             "Export again with Exportify set to English.")
    out = []
    for row in rows:
        get = lambda key: row[where[key]].strip() if key in where and where[key] < len(row) else ""  # noqa: E731
        m = _TRACK_URI.fullmatch(get("uri"))
        if not m or not get("title") or not get("artists"):
            continue
        cover = get("cover_url") or None
        album = _ALBUM_URI.fullmatch(get("album_uri"))
        out.append({
            "id": m[1], "title": get("title"), "artists": split_names(get("artists")),
            "album": get("album") or None, "album_artists": split_names(get("album_artists")),
            "album_id": album[1] if album else None, "release_date": get("release_date") or None,
            "track_number": _int(get("track_number")), "disc_number": _int(get("disc_number")),
            "duration_ms": _int(get("duration_ms")), "isrc": get("isrc") or None,
            "explicit": get("explicit").lower() == "true", "cover_url": cover, "thumb_url": _thumb(cover),
            "added_at": get("added_at") or None,
        })
    return out


def read_upload(filename: str, data: bytes) -> list[tuple[str, list[dict]]]:
    """[(file name without .csv, tracks)] for a .csv, or for each .csv in an "Export All" .zip."""
    if len(data) > MAX_UPLOAD:
        raise ExportifyError("That file is too big (50 MB at most).")
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                files = [(n, z.read(n)) for n in sorted(z.namelist())
                         if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")]
        except zipfile.BadZipFile as e:
            raise ExportifyError(f"That .zip can't be read: {e}") from None
        if not files:
            raise ExportifyError("There are no .csv files in that .zip.")
    else:
        files = [(filename or "playlist.csv", data)]
    out = []
    for name, raw in files:
        text = raw.decode("utf-8-sig", errors="replace")
        out.append((PurePosixPath(name.replace("\\", "/")).stem, read_csv(text)))
    return out


def is_liked(stem: str) -> bool:
    """Exportify's file of Liked Songs: liked.csv (a browser may save another as "liked (1).csv")."""
    return re.fullmatch(r"liked(\s*\(\d+\))?", stem.strip().lower()) is not None


def playlist_name(stem: str) -> str:
    """Exportify names files after the playlist in lower case with _ for spaces and symbols."""
    name = re.sub(r"_+", " ", stem).strip() or "Imported playlist"
    return (name[0].upper() + name[1:])[:100]


def _when(added_at: str | None, fallback: float) -> float:
    try:
        return datetime.fromisoformat((added_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def import_upload(svc: Service, user_id: int, filename: str, data: bytes) -> dict:
    """Import an Exportify .csv or .zip into a profile. Returns a summary for the admin page:
    {liked, liked_new, playlists: [{name, songs}], skipped: [names], new_songs}."""
    files = read_upload(filename, data)
    db, now = svc.db, time.time()
    summary: dict = {"liked": 0, "liked_new": 0, "playlists": [], "skipped": [], "new_songs": 0}
    for stem, tracks in files:
        if not tracks:
            continue
        summary["new_songs"] += db.upsert_tracks(tracks)
        if is_liked(stem):
            before = {r[0] for r in db.q("SELECT track_id FROM likes WHERE user_id = ?", (user_id,))}
            with db.tx() as c:  # file order is newest first: keep it when there's no date
                c.executemany("INSERT OR IGNORE INTO likes(user_id, track_id, created) VALUES (?, ?, ?)",
                              [(user_id, t["id"], _when(t.get("added_at"), now - i)) for i, t in enumerate(tracks)])
                c.executemany("INSERT OR IGNORE INTO track_sources(track_id, source) VALUES (?, ?)",
                              [(t["id"], f"like:{user_id}") for t in tracks])
            summary["liked"] += len(tracks)
            summary["liked_new"] += len({t["id"] for t in tracks} - before)
            continue
        name = playlist_name(stem)
        if db.one("SELECT 1 FROM user_playlists WHERE user_id = ? AND name = ?", (user_id, name)):
            summary["skipped"].append(name)  # brought over before: importing again doesn't double it
            continue
        pid = "up" + secrets.token_hex(8)
        with db.tx() as c:
            c.execute("INSERT INTO user_playlists(id, user_id, name, description, created, updated) VALUES (?,?,?,?,?,?)",
                      (pid, user_id, name, "Imported from Spotify with Exportify", now, now))
            c.executemany("INSERT INTO user_playlist_tracks(playlist_id, position, track_id, added_at) VALUES (?,?,?,?)",
                          [(pid, i, t["id"], _when(t.get("added_at"), now)) for i, t in enumerate(tracks)])
            c.executemany("INSERT OR IGNORE INTO track_sources(track_id, source) VALUES (?, ?)",
                          [(t["id"], f"up:{pid}") for t in tracks])
        summary["playlists"].append({"name": name, "songs": len(tracks)})
    if not summary["liked"] and not summary["playlists"] and not summary["skipped"]:
        raise ExportifyError("There are no Spotify songs in that file.")
    db.recompute_monitored()
    svc.playlists_dirty = True
    svc.wake.set()  # the new songs download
    svc.recommender.schedule_all()
    who = db.one("SELECT name FROM users WHERE id = ?", (user_id,))
    parts = [f"{summary['liked']} liked songs"] if summary["liked"] else []
    parts += [f"{len(summary['playlists'])} playlists"] if summary["playlists"] else []
    db.log("people", f"Imported {' and '.join(parts) or 'nothing new'} from Exportify into {who[0] if who else 'a profile'}'s "
                     f"profile ({summary['new_songs']} songs new to the library)")
    return summary
