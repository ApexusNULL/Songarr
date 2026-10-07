"""Library layout and Windows-safe file names: Artist/Album (Year)/01 - Title.ext"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_REPLACE = [
    (re.compile(r"\s*:\s*"), " - "),  # "Title: Subtitle" -> "Title - Subtitle" (like Sonarr's smart colon)
    (re.compile(r"[\\/|]"), "-"),
    (re.compile(r'"'), "'"),
    (re.compile(r"[<>]"), ""),
    (re.compile(r"[?*]"), ""),
    (re.compile(r"[\x00-\x1f]"), ""),
]


def safe(part: str, limit: int = 120) -> str:
    s = part or "Unknown"
    for rx, rep in _REPLACE:
        s = rx.sub(rep, s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    if len(s) > limit:
        s = s[:limit].rstrip(". ")
    return s or "_"


def year(release_date: str | None) -> str | None:
    return release_date[:4] if release_date and release_date[:4].isdigit() else None


def relative_path(t: dict, ext: str) -> Path:
    album_artist = (t.get("album_artists") or t.get("artists") or ["Unknown Artist"])[0]
    album = t.get("album") or "Unknown Album"
    y = year(t.get("release_date"))
    folder = f"{album} ({y})" if y else album
    n = t.get("track_number") or 0
    disc = t.get("disc_number") or 1
    num = f"{disc}-{n:02d}" if disc > 1 else f"{n:02d}"
    return Path(safe(album_artist)) / safe(folder) / f"{num} - {safe(t.get('title') or 'Unknown', 150)}.{ext}"


def target_path(root: str | Path, t: dict, ext: str) -> Path:
    return Path(root) / relative_path(t, ext)


def place(src: Path, dest: Path) -> Path:
    """Move a finished file into the library (works across drives / to the NAS)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
    src.unlink(missing_ok=True)
    return dest


def write_folder_cover(folder: Path, cover: bytes | None) -> None:
    """cover.jpg next to the album's tracks, for Plex/Jellyfin/Explorer thumbnails."""
    if not cover:
        return
    p = folder / "cover.jpg"
    if not p.exists():
        try:
            p.write_bytes(cover)
        except OSError:
            pass
