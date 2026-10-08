"""Music that's already in the music folder, so it isn't downloaded again.

When Songarr starts, when the music folder changes, once a day, and on request (Settings →
Downloads → Scan now), it looks through the folder for audio files and reads their tags into an
index (later scans only read new or changed files). A song waiting to be downloaded is matched to a
file when the file:
- carries Songarr's own tag for that song (made by Songarr before, say on another PC),
- is the same recording (ISRC), or
- has the same title and artist, and a length within a few seconds (the same album is preferred).
Untagged files are read from their names, as Artist/Album/01 - Title.ext.

Someone else's file is used where it is: never moved, renamed or retagged. A file Songarr keeps
for another song (the same song on another album) is copied into this song's own album folder
instead, as downloads are, so every album folder stays complete.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import library, tagging
from .db import track_dict
from .matching import norm, strip_title
from .tagging import SPOTIFY_ID_KEY

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

AUDIO = {".m4a", ".mp4", ".aac", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav"}  # what phones can play
LENGTH_SLACK = 3.0  # seconds
RESCAN_EVERY = 24 * 3600
BATCH = 200
SKIP = {"$recycle.bin", "@eadir", "system volume information", ".trash", ".trashes", "#recycle"}  # bins, NAS thumbnails
WAITING = ("wanted", "failed", "review")  # songs that would otherwise be downloaded


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = getattr(value, "text", None)  # ID3 frames
    if text is not None:
        return _first(text)
    return str(value).strip()


def read_tags(path: Path) -> dict:
    """Title, artist, album, ISRC, Songarr's own id and length from a file's tags (blank when missing)."""
    import mutagen

    out = {"title": "", "artist": "", "album": "", "isrc": "", "spotify_id": "", "duration": None}
    try:
        f = mutagen.File(path)
    except Exception:
        f = None
    if f is None:
        return out
    if getattr(f, "info", None) is not None and getattr(f.info, "length", None):
        out["duration"] = float(f.info.length)
    tags = f.tags
    if tags is None:
        return out
    name = type(tags).__name__
    if name == "MP4Tags":
        get = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
               "isrc": "----:com.apple.iTunes:ISRC", "spotify_id": f"----:com.apple.iTunes:{SPOTIFY_ID_KEY}"}
        for k, key in get.items():
            out[k] = _first(tags.get(key))
    elif name == "ID3":
        for k, key in {"title": "TIT2", "artist": "TPE1", "album": "TALB", "isrc": "TSRC"}.items():
            out[k] = _first(tags.get(key))
        frames = tags.getall(f"TXXX:{SPOTIFY_ID_KEY}")
        out["spotify_id"] = _first(frames[0]) if frames else ""
    else:  # Vorbis comments: FLAC, Ogg Vorbis, Opus
        for k, key in {"title": "title", "artist": "artist", "album": "album", "isrc": "isrc",
                       "spotify_id": SPOTIFY_ID_KEY.lower()}.items():
            try:
                out[k] = _first(tags.get(key))
            except (KeyError, ValueError):
                pass
    out["isrc"] = out["isrc"].upper().replace("-", "")
    return out


_NUMBER = re.compile(r"^\s*(?:\d{1,2}-)?\d{1,3}\s*(?:[-._]\s*|\s+)")
_YEAR = re.compile(r"\s*[\(\[]\d{4}[\)\]]\s*$")


def from_name(path: Path) -> dict:
    """Artist/Album (Year)/01 - Title.ext, for files without tags."""
    return {"title": _NUMBER.sub("", path.stem).strip(), "artist": path.parent.parent.name,
            "album": _YEAR.sub("", path.parent.name).strip()}


def title_key(title: str) -> str:
    return norm(strip_title(title or ""))


_SEPARATORS = re.compile(r"\s*(?:,|;|/|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\sx\s)\s*", re.I)


def artist_fits(track_artists: list[str], file_artist: str) -> bool:
    """The file's artist tag ("A, B", "A feat. B", "A & B") is led by the song's main artist, or lists it."""
    have = norm(file_artist)
    main = norm(track_artists[0]) if track_artists else ""
    if not main or not have:
        return False
    if have == main or have.startswith(main + " "):
        return True
    parts = {norm(part) for part in _SEPARATORS.split(file_artist)}
    return bool(parts & {norm(a) for a in track_artists})


class Scanner:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.ready = threading.Event()  # the first scan is done: downloads may start
        self.wake = threading.Event()
        self.state: dict = {"running": False, "files": 0, "checked": 0, "found": 0, "finished": None, "error": None,
                            "folder": None}
        self._lock = threading.Lock()

    def info(self) -> dict:
        return dict(self.state) | {"indexed": self.db.one("SELECT COUNT(*) FROM library_files")[0]}

    # -- the index ----------------------------------------------------------------------------------

    def scan(self) -> dict:
        """Bring the index up to date with the music folder, then match waiting songs. Returns the state."""
        with self._lock:
            root = Path(self.db.setting("library_root") or "")
            self.state.update(running=True, files=0, checked=0, found=0, error=None, folder=str(root))
            try:
                if not root.is_dir():
                    raise FileNotFoundError(f"The music folder {root} can't be reached.")
                stamp = time.time()
                known = {r["path"]: (r["size"], r["mtime"]) for r in self.db.q("SELECT path, size, mtime FROM library_files")}
                batch: list[tuple] = []
                seen: list[str] = []
                for path, st in self._walk(root):
                    self.state["files"] += 1
                    key = str(path)
                    seen.append(key)
                    if known.get(key) == (st.st_size, st.st_mtime):
                        continue
                    batch.append(self._row(path, st, stamp))
                    self.state["checked"] += 1
                    if len(batch) >= BATCH:
                        self._save(batch)
                        batch = []
                    if self.svc.stop_event.is_set():
                        return self.state
                self._save(batch)
                gone = set(known) - set(seen)
                with self.db.tx() as c:
                    c.executemany("DELETE FROM library_files WHERE path = ?", [(p,) for p in gone])
                self.state["found"] = self.match_waiting()
                if self.state["found"]:
                    self.db.log("imported", f"Found {self.state['found']:,} song{'s' if self.state['found'] != 1 else ''} "
                                "already in your music folder; they won't be downloaded.")
                    self.svc.playlists_dirty = True
            except OSError as e:
                self.state["error"] = str(e)
                log.warning("music folder scan: %s", e)
            finally:
                self.state.update(running=False, finished=time.time())
                self.ready.set()
                self.svc.wake.set()  # downloads may start (or go on)
                self.db.release()
            return self.state

    def _walk(self, root: Path):
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for e in entries:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if e.name.lower() not in SKIP and not e.name.lower().startswith(".trash-"):
                                    stack.append(Path(e.path))
                            elif Path(e.name).suffix.lower() in AUDIO:
                                yield Path(e.path), e.stat()
                        except OSError:
                            continue
            except OSError:
                continue

    def _row(self, path: Path, st: os.stat_result, stamp: float) -> tuple:
        tags = read_tags(path)
        guess = from_name(path)
        title = tags["title"] or guess["title"]
        artist = tags["artist"] or guess["artist"]
        album = tags["album"] or guess["album"]
        return (str(path), st.st_size, st.st_mtime, title, artist, album, tags["isrc"] or None,
                tags["spotify_id"] or None, tags["duration"], title_key(title), stamp)

    def _save(self, rows: list[tuple]) -> None:
        if rows:
            with self.db.tx() as c:
                c.executemany("""INSERT OR REPLACE INTO library_files(path, size, mtime, title, artist, album, isrc,
                                 spotify_id, duration, title_key, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)

    # -- matching -----------------------------------------------------------------------------------

    def find(self, t: dict) -> Path | None:
        """A file in the music folder that is this song, or None."""
        rows = self.db.q("SELECT * FROM library_files WHERE spotify_id = ? LIMIT 1", (t["id"],))
        if not rows and t.get("isrc"):
            rows = self.db.q("SELECT * FROM library_files WHERE isrc = ?", (t["isrc"].upper(),))
        if not rows:
            key = title_key(t.get("title") or "")
            artists = t["artists"] if isinstance(t["artists"], list) else json.loads(t["artists"] or "[]")
            length = (t.get("duration_ms") or 0) / 1000
            rows = [r for r in self.db.q("SELECT * FROM library_files WHERE title_key = ?", (key,)) if key
                    and artist_fits(artists, r["artist"] or "")
                    and (not length or not r["duration"] or abs(r["duration"] - length) <= LENGTH_SLACK)]
            album = norm(t.get("album") or "")
            rows.sort(key=lambda r: (norm(r["album"] or "") != album, abs((r["duration"] or length) - length)))
        for r in rows:
            path = Path(r["path"])
            if path.is_file():
                return path
        return None

    def take(self, t: dict, quiet: bool = False) -> bool:
        """Mark [t] downloaded if it's already in the music folder."""
        path = self.find(t)
        if path is None:
            return False
        fields = {}
        owner = self.db.one("SELECT youtube_id, match_score, bitrate FROM tracks WHERE file_path = ? AND id != ? LIMIT 1",
                            (str(path), t["id"]))
        if owner is not None and path.suffix.lower() in (".m4a", ".mp3", ".opus"):
            # Songarr's file for another song: a copy in this song's own album folder, tagged for it
            path = self._copy_into_album(t, path, owner["youtube_id"])
            fields = {"youtube_id": owner["youtube_id"], "match_score": owner["match_score"], "bitrate": owner["bitrate"]}
        self.db.set_status(t["id"], "downloaded", file_path=str(path), file_size=path.stat().st_size, error=None,
                           attempts=0, next_attempt=None, blocked=0, **fields)
        if not quiet:
            self.db.log("imported", f"{t['title']}: already in your music folder ({path.name}), not downloaded", t["id"])
        return True

    def _copy_into_album(self, t: dict, src: Path, youtube_id: str | None) -> Path:
        dest = library.target_path(self.db.setting("library_root"), t, src.suffix.lower().lstrip("."))
        if dest.exists() and dest.resolve() == src.resolve():
            return dest
        work = self.svc.tmp / t["id"]
        work.mkdir(parents=True, exist_ok=True)
        try:
            copy = work / src.name
            shutil.copyfile(src, copy)
            cover = tagging.fetch_cover(t.get("cover_url"))
            tagging.tag(copy, t, cover, youtube_id)
            library.place(copy, dest)
            library.write_folder_cover(dest.parent, cover)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return dest

    def match_waiting(self) -> int:
        found = 0
        placeholders = ",".join("?" * len(WAITING))
        # (not a version picked by hand: that's downloaded, whatever the folder has)
        for row in self.db.q(f"""SELECT * FROM tracks WHERE status IN ({placeholders})
                                 AND NOT (status = 'wanted' AND pinned = 1 AND youtube_id IS NOT NULL)""", WAITING):
            if self.svc.stop_event.is_set():
                break
            try:
                if self.take(track_dict(row), quiet=True):
                    found += 1
            except OSError as e:  # one unreadable or unwritable file doesn't stop the rest
                log.warning("couldn't use the existing file for %s: %s", row["title"], e)
        return found

    # -- the background loop ------------------------------------------------------------------------

    def run(self) -> None:
        next_scan = 0.0
        while not self.svc.stop_event.is_set():
            if self.wake.is_set() or time.time() >= next_scan:
                self.wake.clear()
                try:
                    self.scan()
                except Exception:
                    log.exception("music folder scan failed")
                    self.ready.set()
                next_scan = time.time() + RESCAN_EVERY
            self.wake.wait(timeout=60)
