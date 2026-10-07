"""Podcast downloads that clean up after themselves.

Unlike songs, episodes aren't kept forever. Each profile picks, per show, whether new episodes
download and how long they stay: until played, or for a number of days. Single episodes can
be kept the same way. A file stays on the server while anyone still wants it, then goes; the
apps mirror the same list onto the phone for offline listening.

Files live in <podcast_root>/<Show>/<date> - <Episode> [id].<ext> (podcast_root defaults to a
Podcasts folder next to the music library).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from . import podcasts
from .library import safe

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

KEEP_CHOICES = ("off", "played", "3", "7", "14", "30")
AUTO_NEWEST = 2                 # newest episodes per show kept downloaded
AUTO_WITHIN = 30 * 86400        # ...if they came out in the last 30 days
PLAYED_CAP = 90 * 86400         # "until played" still ends after 90 days
MAX_EPISODE_BYTES = 2 * 1024**3
INTERVAL = 30 * 60
STARTUP_DELAY = 20              # seconds; tests raise it and call cycle() themselves

_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
        "audio/ogg": ".ogg", "audio/opus": ".opus", "video/mp4": ".mp4", "video/quicktime": ".mov"}
_KNOWN = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mov", ".wav", ".flac"}


def check_keep(keep: object, allow_off: bool = True) -> str:
    k = str(keep)
    if k not in KEEP_CHOICES or (k == "off" and not allow_off):
        raise ValueError(f"keep must be one of {', '.join(c for c in KEEP_CHOICES if allow_off or c != 'off')}")
    return k


def extension(audio_type: str | None, url: str) -> str:
    if audio_type and audio_type.split(";")[0].strip().lower() in _EXT:
        return _EXT[audio_type.split(";")[0].strip().lower()]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in _KNOWN else ".mp3"


class PodcastDownloads:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()
        self.active: str | None = None  # episode being downloaded right now

    def root(self) -> Path:
        if custom := self.db.setting("podcast_root"):
            return Path(custom)
        return Path(self.db.setting("library_root") or self.svc.data_dir / "Music").parent / "Podcasts"

    # -- choices ---------------------------------------------------------------------

    def keep_for(self, user_id: int, podcast_id: str) -> str:
        row = self.db.one("SELECT keep FROM podcast_prefs WHERE user_id = ? AND podcast_id = ?", (user_id, podcast_id))
        return row["keep"] if row else "off"

    def set_keep(self, user_id: int, podcast_id: str, keep: str) -> None:
        keep = check_keep(keep)
        with self.db.tx() as c:
            if keep == "off":
                c.execute("DELETE FROM podcast_prefs WHERE user_id = ? AND podcast_id = ?", (user_id, podcast_id))
                # turning it off removes what the setting downloaded (episodes kept by hand stay)
                c.execute("""UPDATE episode_holds SET released = ? WHERE user_id = ? AND auto = 1 AND released IS NULL
                             AND episode_id IN (SELECT id FROM podcast_episodes WHERE podcast_id = ?)""",
                          (time.time(), user_id, podcast_id))
            else:
                c.execute("INSERT OR REPLACE INTO podcast_prefs(user_id, podcast_id, keep) VALUES (?,?,?)", (user_id, podcast_id, keep))
                c.execute("""UPDATE episode_holds SET keep = ? WHERE user_id = ? AND auto = 1 AND released IS NULL
                             AND episode_id IN (SELECT id FROM podcast_episodes WHERE podcast_id = ?)""", (keep, user_id, podcast_id))
        self.wake.set()

    def hold(self, user_id: int, episode_id: str, keep: str) -> None:
        """Keep one episode downloaded (until played, or for some days from now)."""
        keep = check_keep(keep, allow_off=False)
        self.db.run("""INSERT INTO episode_holds(user_id, episode_id, keep, created, released, auto) VALUES (?,?,?,?,NULL,0)
                       ON CONFLICT(user_id, episode_id) DO UPDATE SET keep = excluded.keep, created = excluded.created,
                         released = NULL, auto = 0""", (user_id, episode_id, keep, time.time()))
        self.wake.set()

    def unhold(self, user_id: int, episode_id: str) -> None:
        self.db.run("UPDATE episode_holds SET released = ? WHERE user_id = ? AND episode_id = ? AND released IS NULL",
                    (time.time(), user_id, episode_id))
        self.wake.set()

    def info(self, user_id: int, episode_ids: list[str]) -> dict[str, dict]:
        """Per episode: how this profile keeps it, when that ends, and whether the server has the file."""
        if not episode_ids:
            return {}
        marks = ",".join("?" * len(episode_ids))
        out: dict[str, dict] = {}
        for r in self.db.q(f"""SELECT episode_id, keep, created FROM episode_holds WHERE user_id = ? AND released IS NULL
                               AND episode_id IN ({marks})""", [user_id, *episode_ids]):
            out[r["episode_id"]] = {"keep": r["keep"], "expires": self.expires(r["keep"], r["created"])}
        for r in self.db.q(f"SELECT episode_id, size FROM episode_files WHERE episode_id IN ({marks})", episode_ids):
            out.setdefault(r["episode_id"], {}).update(on_server=True, size=r["size"])
        return out

    @staticmethod
    def expires(keep: str, created: float) -> float | None:
        return created + int(keep) * 86400 if keep.isdigit() else None

    # -- the background loop ------------------------------------------------------------

    def run(self) -> None:
        stop = self.svc.stop_event
        if stop.wait(STARTUP_DELAY):
            return
        while not stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("podcast downloads failed")
            self.wake.wait(INTERVAL)
            self.wake.clear()
        self.db.release()

    def cycle(self) -> None:
        self.plan()
        self.release_finished()
        self.fetch_missing()
        self.prune()

    def plan(self) -> None:
        """Hold the newest episodes of shows set to download (once each: an episode you finish or
        that runs out of days isn't fetched again)."""
        now = time.time()
        for pref in self.db.q("SELECT user_id, podcast_id, keep FROM podcast_prefs WHERE keep != 'off'"):
            try:
                self.svc.podcasts.refresh(pref["podcast_id"], 3600)
            except (podcasts.PodcastError, OSError, ValueError) as e:
                log.info("podcast %s: feed unavailable: %s", pref["podcast_id"], e)
            for e in self.db.q("""SELECT id FROM podcast_episodes WHERE podcast_id = ? AND published > ?
                                  AND id NOT IN (SELECT episode_id FROM episode_progress WHERE user_id = ? AND completed = 1)
                                  ORDER BY published DESC LIMIT ?""",
                               (pref["podcast_id"], now - AUTO_WITHIN, pref["user_id"], AUTO_NEWEST)):
                self.db.run("""INSERT OR IGNORE INTO episode_holds(user_id, episode_id, keep, created, released, auto)
                               VALUES (?,?,?,?,NULL,1)""", (pref["user_id"], e["id"], pref["keep"], now))

    def release_finished(self) -> None:
        now = time.time()
        self.db.run("""UPDATE episode_holds SET released = ? WHERE released IS NULL AND (
                         (keep = 'played' AND (created < ? OR EXISTS(SELECT 1 FROM episode_progress p
                              WHERE p.user_id = episode_holds.user_id AND p.episode_id = episode_holds.episode_id AND p.completed = 1)))
                         OR (keep != 'played' AND created + CAST(keep AS REAL) * 86400 < ?)
                         OR episode_id NOT IN (SELECT id FROM podcast_episodes))""", (now, now - PLAYED_CAP, now))

    def fetch_missing(self) -> None:
        rows = self.db.q("""SELECT e.id, e.title, e.audio_url, e.audio_type, e.published, p.title AS show
                            FROM podcast_episodes e JOIN podcasts p ON p.id = e.podcast_id
                            WHERE e.id IN (SELECT episode_id FROM episode_holds WHERE released IS NULL)
                            AND e.id NOT IN (SELECT episode_id FROM episode_files)
                            ORDER BY (SELECT MIN(created) FROM episode_holds h WHERE h.episode_id = e.id)""")
        for r in rows:
            if self.svc.stop_event.is_set():
                break
            self.fetch(r)

    def path_for(self, r) -> Path:
        date = time.strftime("%Y-%m-%d", time.gmtime(r["published"] or time.time()))
        name = f"{date} - {safe(r['title'], 100)} [{r['id'][2:10]}]{extension(r['audio_type'], r['audio_url'])}"
        return self.root() / safe(r["show"] or "Podcast", 80) / name

    def fetch(self, r) -> bool:
        dest = self.path_for(r)
        part = dest.with_name(dest.name + ".part")
        self.active = r["id"]
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with podcasts.open_url(r["audio_url"], timeout=60) as up, part.open("wb") as f:
                while chunk := up.read(256 * 1024):
                    total += len(chunk)
                    if total > MAX_EPISODE_BYTES:
                        raise podcasts.PodcastError("episode is larger than 2 GB")
                    if self.svc.stop_event.is_set():
                        raise podcasts.PodcastError("Songarr is stopping")
                    f.write(chunk)
            os.replace(part, dest)
            self.db.run("INSERT OR REPLACE INTO episode_files(episode_id, path, size, downloaded) VALUES (?,?,?,?)",
                        (r["id"], str(dest), total, time.time()))
            self.db.log("podcast", f"Downloaded {r['show']}: {r['title']}")
            return True
        except (podcasts.PodcastError, OSError) as e:
            log.warning("couldn't download episode %s (%s): %s", r["id"], r["title"], e)
            part.unlink(missing_ok=True)
            return False
        finally:
            self.active = None

    def prune(self) -> None:
        """Delete files nobody keeps any more (and forget files that vanished from disk)."""
        root = self.root()
        for f in self.db.q("""SELECT episode_id, path FROM episode_files WHERE episode_id NOT IN
                              (SELECT episode_id FROM episode_holds WHERE released IS NULL)"""):
            path = Path(f["path"])
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("couldn't delete %s: %s", path, e)
                continue
            self.db.run("DELETE FROM episode_files WHERE episode_id = ?", (f["episode_id"],))
            if path.parent != root and path.parent.is_dir() and root in path.parents:
                try:
                    path.parent.rmdir()  # only succeeds once the show's folder is empty
                except OSError:
                    pass
        for f in self.db.q("SELECT episode_id, path FROM episode_files"):
            if not Path(f["path"]).exists():
                self.db.run("DELETE FROM episode_files WHERE episode_id = ?", (f["episode_id"],))
