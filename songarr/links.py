"""Songs from links: a YouTube video, or anything else yt-dlp can download (SoundCloud, Bandcamp, a
video on another site...), pasted or shared into the app (POST /requests/link). That very upload is
what downloads: no searching, no matching against a catalogue.

What the song is called comes from the page: YouTube Music's own song details when it has them,
otherwise the video's title, read as "Artist - Title" when it looks like that, with "(Official
Video)" and the like taken off, and the channel as the artist failing that.
"""

from __future__ import annotations

import hashlib
import re
import time

YOUTUBE = ("Youtube", "YoutubeMusic")
_NOISE = re.compile(r"\s*[(\[](?:official\s+)?(?:music\s+|lyrics?\s+|hd\s+|4k\s+)?"
                    r"(?:video|audio|visuali[sz]er|lyrics?|clip|mv|m/v|hd|hq|4k)(?:\s+video)?[)\]]", re.I)
_ARTIST_TITLE = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$")


def is_youtube(info: dict) -> bool:
    return info.get("extractor_key") in YOUTUBE or info.get("ie_key") in YOUTUBE


def track_id(info: dict, url: str) -> str:
    """Songarr's id for it (letters and digits only): the same upload always gets the same one."""
    if is_youtube(info) and info.get("id"):
        return "yt" + info["id"].encode().hex()
    return "ln" + hashlib.sha1((info.get("webpage_url") or url).encode()).hexdigest()[:24]


def link_track(info: dict, url: str) -> dict:
    """A song for the library from what yt-dlp read at the link."""
    title = (info.get("track") or info.get("title") or "").strip() or "Untitled"
    artists = [a for a in info.get("artists") or [] if a] or [a for a in (info.get("artist"), info.get("creator")) if a]
    if not artists and not info.get("track") and (m := _ARTIST_TITLE.match(title)):
        artists, title = [m.group(1).strip()], m.group(2).strip()
    if not artists:
        channel = info.get("channel") or info.get("uploader") or "Unknown artist"
        artists = [re.sub(r"\s+-\s+Topic$", "", channel)]
    title = _NOISE.sub("", title).strip() or title
    day = info.get("release_date") or info.get("upload_date") or ""
    if is_youtube(info) and info.get("id"):
        cover = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"
    else:
        thumbs = [t.get("url") for t in info.get("thumbnails") or [] if t.get("url")]
        cover = info.get("thumbnail") or (thumbs[-1] if thumbs else None)
    duration = info.get("duration")
    return {
        "id": track_id(info, url), "title": title, "artists": artists, "album": info.get("album"),
        "album_artists": artists[:1], "album_id": None,
        "release_date": f"{day[:4]}-{day[4:6]}-{day[6:8]}" if re.fullmatch(r"\d{8}", day) else None,
        "track_number": None, "disc_number": 1, "duration_ms": int(duration * 1000) if duration else None,
        "isrc": None, "explicit": False, "cover_url": cover, "thumb_url": cover,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
