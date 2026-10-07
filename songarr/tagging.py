"""Write a song's details (title, artists, album, ISRC...) and cover art into M4A, MP3 or Opus files."""

from __future__ import annotations

import base64
import urllib.request
from pathlib import Path

from mutagen.flac import Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus

from .library import year

SPOTIFY_ID_KEY = "SPOTIFY_TRACK_ID"
YOUTUBE_ID_KEY = "YOUTUBE_ID"


def fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read()
    except OSError:
        return None


def tag(path: Path, t: dict, cover: bytes | None, youtube_id: str | None = None) -> None:
    artists = t.get("artists") or []
    album_artists = t.get("album_artists") or artists[:1]
    fields = {
        "title": t.get("title") or "",
        "artist": ", ".join(artists),
        "album_artist": ", ".join(album_artists),
        "album": t.get("album") or "",
        "date": t.get("release_date") or "",
        "track": t.get("track_number") or 0,
        "disc": t.get("disc_number") or 1,
        "isrc": t.get("isrc") or "",
    }
    ext = path.suffix.lower()
    if ext == ".m4a":
        a = MP4(path)
        a["\xa9nam"], a["\xa9ART"], a["aART"], a["\xa9alb"] = fields["title"], fields["artist"], fields["album_artist"], fields["album"]
        if fields["date"]:
            a["\xa9day"] = fields["date"]
        a["trkn"] = [(fields["track"], 0)]
        a["disk"] = [(fields["disc"], 0)]
        if t.get("explicit"):
            a["rtng"] = [1]
        free = {"ISRC": fields["isrc"], SPOTIFY_ID_KEY: t.get("id") or "", YOUTUBE_ID_KEY: youtube_id or ""}
        for k, v in free.items():
            if v:
                a[f"----:com.apple.iTunes:{k}"] = [MP4FreeForm(v.encode())]
        if cover:
            a["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if cover[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG)]
        a.save()
    elif ext == ".mp3":
        try:
            a = ID3(path)
        except Exception:
            a = ID3()
        a.add(TIT2(encoding=3, text=fields["title"]))
        a.add(TPE1(encoding=3, text=fields["artist"]))
        a.add(TPE2(encoding=3, text=fields["album_artist"]))
        a.add(TALB(encoding=3, text=fields["album"]))
        if y := year(fields["date"]):
            a.add(TDRC(encoding=3, text=fields["date"] or y))
        a.add(TRCK(encoding=3, text=str(fields["track"])))
        a.add(TPOS(encoding=3, text=str(fields["disc"])))
        if fields["isrc"]:
            a.add(TSRC(encoding=3, text=fields["isrc"]))
        a.add(TXXX(encoding=3, desc=SPOTIFY_ID_KEY, text=t.get("id") or ""))
        if youtube_id:
            a.add(TXXX(encoding=3, desc=YOUTUBE_ID_KEY, text=youtube_id))
        if cover:
            a.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        a.save(path, v2_version=3)
    elif ext == ".opus":
        a = OggOpus(path)
        a["title"], a["artist"], a["albumartist"], a["album"] = fields["title"], fields["artist"], fields["album_artist"], fields["album"]
        a["date"] = fields["date"]
        a["tracknumber"], a["discnumber"] = str(fields["track"]), str(fields["disc"])
        if fields["isrc"]:
            a["isrc"] = fields["isrc"]
        a[SPOTIFY_ID_KEY.lower()] = t.get("id") or ""
        if youtube_id:
            a[YOUTUBE_ID_KEY.lower()] = youtube_id
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
            a["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
        a.save()
    else:
        raise ValueError(f"cannot tag {ext} files")


def read_spotify_id(path: Path) -> str | None:
    """The Spotify id Songarr stored in a file, if any (used to recognise files already on disk)."""
    try:
        ext = path.suffix.lower()
        if ext == ".m4a":
            v = MP4(path).get(f"----:com.apple.iTunes:{SPOTIFY_ID_KEY}")
            return bytes(v[0]).decode() if v else None
        if ext == ".mp3":
            frames = ID3(path).getall(f"TXXX:{SPOTIFY_ID_KEY}")
            return str(frames[0].text[0]) if frames else None
        if ext == ".opus":
            v = OggOpus(path).get(SPOTIFY_ID_KEY.lower())
            return v[0] if v else None
    except Exception:
        return None
    return None
