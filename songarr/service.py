"""The running service: a pool of paced, parallel download workers for the songs people want."""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import library, playlists, tagging
from .db import DB, has_file, track_dict
from .matching import Candidate, TrackInfo
from .app_build import AppBuild
from .app_updates import AppUpdates
from .branding import Branding
from .releases import Releases
from .scan import Scanner
from .artist_info import ArtistInfo
from .dependencies import Dependencies
from .jams import Jams
from .lyrics import Lyrics
from .push import Push
from .podcast_downloads import PodcastDownloads
from .podcasts import Podcasts
from .recommend import Recommender
from .users import Users
from .verify import YouTubeSignIn
from .youtube import FORMATS, Blocked, NeedsSignIn, Unavailable, YouTube, find_ffmpeg

log = logging.getLogger(__name__)

RETRY_DELAYS = [3600, 6 * 3600, 24 * 3600, 3 * 86400]  # after 1st, 2nd, 3rd, 4th failure; then give up
COOLDOWN = {"bot": 30 * 60, "throttle": 10 * 60}  # first pause; doubles with each block in a row
MAX_COOLDOWN = 6 * 3600
MIN_RATE = 0.25  # after blocks, pace never drops below a quarter of the configured songs/hour
MAX_BLOCKED_TRIES = 3  # a song that keeps triggering 403s is treated as failed, not as throttling
MAX_WORKERS = 8
REQUEST_PRIORITY = 10  # songs requested from the app download before the backlog
NEEDS_SIGNIN = "Needs YouTube sign-in: YouTube only plays this song to signed-in users (Settings → YouTube sign-in)."


class Stopping(Exception):
    pass


class Pacer:
    """Spaces YouTube requests across all workers.

    Downloads start at most `max_per_hour` x rate_factor times an hour (rate_factor drops after
    YouTube pushes back and recovers with successes); searches may run three times as often.
    Nothing is sent during a cooldown.
    """

    def __init__(self, svc: "Service"):
        self.svc = svc
        self.lock = threading.Lock()
        self.next = {"search": 0.0, "download": 0.0}

    def interval(self, kind: str) -> float:
        per_hour = max(10, int(self.svc.db.setting("max_per_hour") or 250)) * self.svc.rate_factor
        base = 3600 / per_hour
        return base if kind == "download" else max(1.0, base / 3)

    def wait(self, kind: str, on_wait: Callable[[float], None] = lambda s: None) -> None:
        while True:
            with self.lock:
                now = time.time()
                start = max(now, self.next[kind], self.svc.cooldown_until)
                self.next[kind] = start + self.interval(kind) * random.uniform(0.85, 1.15)
            delay = start - time.time()
            if delay > 0.5:
                on_wait(delay)
            if delay > 0 and self.svc.stop_event.wait(delay):
                raise Stopping()
            if time.time() >= self.svc.cooldown_until:  # a block may have started while we waited
                return


class Service:
    def __init__(self, data_dir: Path, port: int, youtube_factory: Callable[[dict], YouTube] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.cluster = None  # cluster.Cluster, when this is one of several servers
        self.standby = False  # another server is active: this one doesn't download or answer the apps
        self.tmp_root = self.data_dir / "tmp"
        self.tmp = self.tmp_root / f"run-{os.getpid()}-{int(time.time())}-{random.randrange(16**4):04x}"
        self.db = DB(self.data_dir / "songarr.db")
        self.users = Users(self.db)
        self.podcasts = Podcasts(self.db)
        self.podcast_downloads = PodcastDownloads(self)
        self.app_updates = AppUpdates(self.data_dir)
        self.branding = Branding(self.db, self.data_dir)
        self.scanner = Scanner(self)  # music already in the music folder isn't downloaded again
        self.releases = Releases(self)  # followed artists' new releases
        self.app_build = AppBuild(self)
        self.artist_info = ArtistInfo(self)
        self.lyrics = Lyrics(self.db)
        self.jams = Jams()
        self.dependencies = Dependencies(self)
        self.last_app_request = 0.0  # when a phone last used the app API (updates restart only when it's quiet)
        self.draining = False  # finishing the downloads in progress, starting no new ones (before a restart)
        self.restart_hook: Callable[[], None] | None = None  # set by __main__: restart Songarr
        self.push = Push(self.db, self.data_dir)
        self.recommender = Recommender(self)
        self.youtube_signin = YouTubeSignIn(self.data_dir)
        self.youtube_factory = youtube_factory or self._default_youtube
        self.active: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.paused = False
        # YouTube throttling state survives restarts, so a restart can't hammer YouTube again.
        self.cooldown_until = float(self.db.setting("yt_cooldown_until") or 0)
        self.rate_factor = float(self.db.setting("yt_rate_factor") or 1.0)
        self.blocks = int(self.db.setting("yt_blocks") or 0)
        self.successes = 0
        self.pacer = Pacer(self)
        self.playlists_dirty = True
        self._playlists_written = 0.0
        self.pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="worker")

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        n = self.db.reset_busy()
        if n:
            log.info("re-queued %d tracks that were in progress at shutdown", n)
        # Leftovers of earlier runs; one finished more than an hour ago can't still be writing.
        for d in self.tmp_root.glob("*") if self.tmp_root.exists() else []:
            if d != self.tmp and time.time() - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        self.threads = [threading.Thread(target=self._dispatcher, name="dispatcher", daemon=True),
                        threading.Thread(target=self.recommender.run, name="recommend", daemon=True),
                        threading.Thread(target=self.podcast_downloads.run, name="podcast-downloads", daemon=True),
                        threading.Thread(target=self.artist_info.run, name="artist-info", daemon=True),
                        threading.Thread(target=self.dependencies.run, name="dependencies", daemon=True),
                        threading.Thread(target=self.scanner.run, name="music-scan", daemon=True),
                        threading.Thread(target=self.releases.run, name="releases", daemon=True)]
        for t in self.threads:
            t.start()

    def start_standby(self) -> None:
        """A standby (backup) server only keeps itself up to date (its code and packages) until it's
        needed: becoming active restarts it into start()."""
        self.threads = [threading.Thread(target=self.dependencies.run, name="dependencies", daemon=True)]
        for t in self.threads:
            t.start()

    def stop(self, wait: bool = False) -> None:
        self.stop_event.set()
        self.wake.set()
        self.podcast_downloads.wake.set()
        self.artist_info.wake()
        self.dependencies.wake.set()
        self.scanner.wake.set()
        self.releases.wake.set()
        if wait:
            for t in getattr(self, "threads", []):
                t.join(timeout=15)
        self.pool.shutdown(wait=wait, cancel_futures=True)
        if wait:
            self.db.close()

    def write_playlist_files(self) -> None:
        self.playlists_dirty = False
        self._playlists_written = time.time()
        if not self.db.setting("write_playlist_files"):
            return
        try:
            n = playlists.write_all(self.db, self.db.setting("library_root"))
            if n:
                log.info("updated %d playlist files", n)
        except OSError as e:
            log.warning("could not write playlist files: %s", e)

    # -- YouTube throttling ---------------------------------------------------------

    def _persist_throttle(self) -> None:
        self.db.set_setting("yt_cooldown_until", self.cooldown_until)
        self.db.set_setting("yt_rate_factor", self.rate_factor)
        self.db.set_setting("yt_blocks", self.blocks)

    def _on_blocked(self, e: Blocked, t: dict) -> None:
        tid = t["id"]
        with self.lock:
            if time.time() >= self.cooldown_until:  # first worker to hit it sets the pause
                self.blocks += 1
                pause = min(MAX_COOLDOWN, COOLDOWN.get(e.kind, COOLDOWN["bot"]) * 2 ** (self.blocks - 1))
                self.cooldown_until = time.time() + pause
                self.rate_factor = max(MIN_RATE, self.rate_factor * 0.5)
                self.successes = 0
                self._persist_throttle()
                per_hour = int(int(self.db.setting("max_per_hour") or 250) * self.rate_factor)
                what = "asked to confirm this isn't a bot" if e.kind == "bot" else "started refusing downloads (HTTP 403)"
                self.db.log("paused", f"YouTube {what}. Pausing {pause // 60} min, then continuing at "
                            f"{per_hour} songs/hour. ({str(e)[:160]})", tid)
                log.warning("YouTube %s; pausing %d min, pace now %d/hour", e.kind, pause // 60, per_hour)
        tries = (t.get("blocked") or 0) + 1
        if e.kind == "throttle" and tries >= MAX_BLOCKED_TRIES:
            refused = RuntimeError(f"YouTube refused this upload {tries} times (HTTP 403)")
            if not self._keep_old(t, refused):
                self._fail(t, refused)
                self.db.set_status(tid, "failed", blocked=0)
        else:
            self.db.set_status(tid, "wanted", blocked=tries, error="Waiting: YouTube asked Songarr to slow down")

    def _on_success(self) -> None:
        with self.lock:
            self.successes += 1
            changed = False
            if self.successes % 20 == 0 and self.rate_factor < 1.0:
                self.rate_factor = min(1.0, self.rate_factor * 1.25)
                changed = True
            if self.successes >= 30 and self.blocks:
                self.blocks = 0
                changed = True
            if changed:
                self._persist_throttle()

    def retry_needing_signin(self) -> int:
        n = self.db.run("UPDATE tracks SET status = 'wanted', attempts = 0, next_attempt = NULL, error = NULL "
                        "WHERE status = 'failed' AND (error = ? OR error LIKE '%confirm your age%' OR error LIKE '%Please sign in%' "
                        "OR error LIKE '%still wants a sign-in%')",
                        (NEEDS_SIGNIN,))
        self.wake.set()
        return n

    def resume(self) -> None:
        self.paused = False
        self.cooldown_until = 0.0
        self._persist_throttle()
        self.wake.set()

    # -- dispatching --------------------------------------------------------------

    def workers(self) -> int:
        return max(1, min(MAX_WORKERS, int(self.db.setting("workers") or 4)))

    def _dispatcher(self) -> None:
        while not self.stop_event.is_set():
            self.wake.wait(timeout=5)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            if self.playlists_dirty and time.time() - self._playlists_written > 60:
                self.write_playlist_files()
            if self.paused or self.draining or not self.scanner.ready.is_set() or time.time() < self.cooldown_until:
                continue  # (until the first scan of the music folder is done, it may already have the songs)
            while len(self.active) < self.workers():
                row = self.db.claim_next()
                if row is None:
                    break
                t = track_dict(row)
                with self.lock:
                    self.active[t["id"]] = {"id": t["id"], "title": t["title"], "artists": t["artists"],
                                            "album": t["album"], "cover_url": t["thumb_url"] or t["cover_url"], "stage": "queued",
                                            "percent": 0.0, "speed": None, "eta": None, "started": time.time()}
                self.pool.submit(self._run, t)

    def _stage(self, tid: str, stage: str, **kw) -> None:
        with self.lock:
            if tid in self.active:
                self.active[tid].update(stage=stage, **kw)

    def _keep_old(self, t: dict, e: Exception) -> bool:
        """Another version chosen for a song that has a file, and it won't download: the song keeps the
        file (and the upload) it had. False when there's no file to keep."""
        if not (t.get("pinned") and t.get("file_path") and Path(t["file_path"]).is_file()):
            return False
        self.db.set_status(t["id"], "downloaded", pinned=0, youtube_id=t.get("previous_youtube_id"), previous_youtube_id=None,
                           error=None, attempts=0, next_attempt=None, blocked=0)
        self.db.log("failed", f"{t['title']}: the version chosen didn't download ({str(e)[:200]}); it keeps the one it had", t["id"])
        return True

    def _fail(self, t: dict, e: Exception) -> None:
        if self._keep_old(t, e):
            return
        attempts = (t.get("attempts") or 0) + 1
        delay = RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else None
        self.db.set_status(t["id"], "failed", error=str(e)[:500], attempts=attempts,
                           next_attempt=time.time() + delay if delay else None)
        self.db.log("failed", f"{t['title']}: {str(e)[:300]}", t["id"])
        log.warning("%s failed: %s", t["title"], e)

    def _run(self, t: dict) -> None:
        tid = t["id"]
        try:
            self.process(t)
        except Stopping:
            self.db.set_status(tid, "wanted")
        except Blocked as e:
            self._on_blocked(e, t)
        except NeedsSignIn as e:
            if self.db.setting("cookies_file"):
                self._fail(t, RuntimeError("YouTube still wants a sign-in for this song; the saved session may have expired."))
            elif not self._keep_old(t, e):  # pointless to retry until someone signs in; signing in retries these
                self.db.set_status(tid, "failed", error=NEEDS_SIGNIN, next_attempt=None)
                self.db.log("failed", f"{t['title']}: needs a signed-in YouTube account", tid)
        except Exception as e:  # any failure: record it and retry later with backoff
            self._fail(t, e)
        finally:
            with self.lock:
                self.active.pop(tid, None)
            shutil.rmtree(self.tmp / tid, ignore_errors=True)
            self.wake.set()

    # -- one track ----------------------------------------------------------------

    def _default_youtube(self, s: dict) -> YouTube:
        return YouTube(ffmpeg=find_ffmpeg(s.get("ffmpeg_path") or ""), cookies_file=s.get("cookies_file") or "")

    def process(self, t: dict) -> None:
        s = self.db.settings()
        tid = t["id"]
        fmt = s["audio_format"] if s["audio_format"] in FORMATS else "m4a"
        ext = FORMATS[fmt][1]
        dest = library.target_path(s["library_root"], t, ext)
        picked = bool(t.get("pinned") and t.get("youtube_id"))  # a version chosen by hand: download it, whatever's on disk

        if not picked and dest.exists() and tagging.read_spotify_id(dest) == tid:  # already on disk (e.g. DB was reset)
            self.db.set_status(tid, "downloaded", file_path=str(dest), file_size=dest.stat().st_size, error=None)
            self.db.log("imported", f"Found existing file for {t['title']}", tid)
            self.playlists_dirty = True
            return

        if not picked and self.scanner.take(t):  # already in the music folder, from anywhere
            self.playlists_dirty = True
            return

        if not picked and self._reuse_same_recording(t, dest):
            return

        if not (t.get("title") or "").strip():
            raise Unavailable("This song is listed without a title or artist, so there's nothing to search for.")
        yt = self.youtube_factory(s)
        yt.pace = lambda kind: self.pacer.wait(
            kind, on_wait=lambda secs: self._stage(tid, f"waiting {secs:.0f}s (pacing for YouTube)"))
        info = TrackInfo(t["title"], t["artists"], t.get("album"), (t.get("duration_ms") or 0) / 1000 or None, t.get("isrc"))
        threshold = float(s.get("match_threshold") or 0.7)

        if t.get("pinned") and t.get("youtube_id"):
            self._stage(tid, "checking your pick")
            chosen = [yt.details(t["youtube_id"], "manual")]
            chosen[0].score = 1.0
        else:
            best, ranked = yt.find(info, threshold, on_stage=lambda st: self._stage(tid, st))
            self.db.save_candidates(tid, ranked)
            if not best:
                raise Unavailable("no YouTube uploads found for this song")
            if best.score < threshold:
                self.db.set_status(tid, "review", match_score=best.score, youtube_id=best.id, blocked=0,
                                   error=f"Best match only scored {best.score:.2f}: pick one under Wanted → Review")
                self.db.log("review", f"{t['title']}: no confident match (best {best.score:.2f}, '{best.title}')", tid)
                return
            chosen = [c for c in ranked if c.score >= threshold]

        self.db.set_status(tid, "downloading")
        last_err: Exception | None = None
        for cand in chosen[:3]:  # fall back to the next good match if an upload won't download
            try:
                path, ytinfo = yt.download(cand.id, self.tmp / tid, fmt, self._progress_hook(tid))
            except (Unavailable, NeedsSignIn) as e:  # another good upload may not be restricted
                last_err = e
                continue
            self._stage(tid, "tagging", percent=100.0)
            cover = tagging.fetch_cover(t.get("cover_url"))
            tagging.tag(path, t, cover, cand.id)
            self._stage(tid, "moving to library")
            final = library.place(path, dest)  # (over the old file, when it's the same path)
            library.write_folder_cover(final.parent, cover)
            if t.get("file_path") and Path(t["file_path"]) != final:
                self._remove_replaced(Path(t["file_path"]), final, tid)
            self.db.set_status(tid, "downloaded", youtube_id=cand.id, match_score=cand.score, file_path=str(final),
                               file_size=final.stat().st_size, bitrate=ytinfo.get("abr"), error=None, attempts=0,
                               next_attempt=None, blocked=0, previous_youtube_id=None)
            self.db.log("downloaded", f"{', '.join(t['artists'])} - {t['title']} ({_describe(cand)})", tid)
            self.playlists_dirty = True
            self._on_success()
            return
        raise last_err or RuntimeError("download failed")

    def _reuse_same_recording(self, t: dict, dest: Path) -> bool:
        """Spotify sometimes lists one recording under several IDs (an album and its re-release).
        Same ISRC means same recording: reuse the file already downloaded instead of asking
        YouTube again. Same target path: share the file. Different album folder: copy it locally
        and tag the copy with this entry's metadata."""
        if not t.get("isrc"):
            return False
        twin = self.db.one(
            """SELECT id, title, file_path, youtube_id, match_score, bitrate FROM tracks
               WHERE isrc = ? AND id != ? AND status = 'downloaded' AND file_path IS NOT NULL""",
            (t["isrc"], t["id"]),
        )
        if twin is None:
            return False
        src = Path(twin["file_path"])
        if not src.exists() or src.suffix.lower() != dest.suffix.lower():
            return False
        tid = t["id"]
        if src.resolve() != dest.resolve():
            self._stage(tid, "copying the same recording")
            work = self.tmp / tid
            work.mkdir(parents=True, exist_ok=True)
            copy = work / src.name
            shutil.copyfile(src, copy)
            cover = tagging.fetch_cover(t.get("cover_url"))
            tagging.tag(copy, t, cover, twin["youtube_id"])
            library.place(copy, dest)
            library.write_folder_cover(dest.parent, cover)
        self.db.set_status(tid, "downloaded", file_path=str(dest), file_size=dest.stat().st_size, youtube_id=twin["youtube_id"],
                           match_score=twin["match_score"], bitrate=twin["bitrate"], error=None, attempts=0, next_attempt=None)
        self.db.log("imported", f"{t['title']}: same recording as an already-downloaded entry (ISRC {t['isrc']}), "
                    "reused without downloading", tid)
        self.playlists_dirty = True
        return True

    def _progress_hook(self, tid: str) -> Callable[[dict], None]:
        def hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = 100.0 * d.get("downloaded_bytes", 0) / total if total else 0.0
                self._stage(tid, "downloading", percent=round(pct, 1), speed=d.get("speed"), eta=d.get("eta"))
            elif d.get("status") == "finished":
                self._stage(tid, "converting", percent=100.0)
        return hook

    # -- user actions --------------------------------------------------------------

    def retry(self, tid: str) -> None:
        self.db.set_status(tid, "wanted", error=None, attempts=0, next_attempt=None, pinned=0, blocked=0)
        self.wake.set()

    def retry_review(self) -> int:
        """Search YouTube again for every song in Needs review (Wanted → Needs review → Retry all).
        Songs that still have no confident match come back there, with fresh uploads to pick from."""
        n = self.db.run("""UPDATE tracks SET status = 'wanted', error = NULL, attempts = 0, next_attempt = NULL, pinned = 0,
                           blocked = 0, updated = ? WHERE status = 'review' AND monitored = 1""", (time.time(),))
        if n:
            self.db.log("retry", f"Searching again for {n} song{'s' if n != 1 else ''} that needed review")
        self.wake.set()
        return n

    def delete_file(self, tid: str, download_again: bool = False) -> str | None:
        """Delete a song's file from the music library (from the admin site). Unless it's to be
        downloaded again (a bad copy), the song is marked ignored so it isn't fetched straight
        back for whoever likes it; Unignore brings it back. People's likes and playlists keep
        it. Empty album and artist folders left behind are removed. Returns the deleted path."""
        row = self.db.one("SELECT title, file_path FROM tracks WHERE id = ?", (tid,))
        if row is None:
            raise ValueError("Unknown song.")
        deleted = None
        if row["file_path"]:
            path = Path(row["file_path"])
            root = Path(self.db.setting("library_root") or "").resolve()
            if not root.is_dir() or root == root.parent or not path.resolve().is_relative_to(root):
                raise ValueError("That file isn't in the music library folder, so Songarr won't delete it.")
            path.unlink(missing_ok=True)
            deleted = str(path)
            for folder in (path.parent, path.parent.parent):  # album, then artist folder, if now empty
                if folder.resolve() == root or not folder.resolve().is_relative_to(root):
                    break
                try:
                    folder.rmdir()
                except OSError:
                    break  # not empty
        fields = {"file_path": None, "file_size": None, "bitrate": None, "error": None}
        if download_again:
            self.db.set_status(tid, "wanted", attempts=0, next_attempt=None, pinned=0, blocked=0, **fields)
            self.wake.set()
        else:
            self.db.set_status(tid, "ignored", **fields)
        self.db.log("deleted", f"File deleted from the server{', downloading it again' if download_again else ''}"
                               f"{f' ({deleted})' if deleted else ''}", tid)
        self.playlists_dirty = True  # playlist files only list songs that have a file
        return deleted

    def pick(self, tid: str, youtube_id: str) -> None:
        self.replace(tid, youtube_id)

    def replace(self, tid: str, youtube_id: str, user_id: int | None = None) -> None:
        """Download this YouTube upload for the song (it got the wrong recording: a cover, a live
        version...), ahead of everything else. The new file replaces the old one; if it won't download,
        the old one stays. From the admin site (Pick) or the app (Replace), by anyone in the family."""
        row = self.db.one("SELECT title, artists, status, youtube_id, pinned, previous_youtube_id, file_path FROM tracks WHERE id = ?",
                          (tid,))
        if row is None:
            raise ValueError("Unknown song.")
        if tid in self.active or row["status"] in ("searching", "downloading"):  # (claimed, maybe not yet in active)
            raise RuntimeError("That song is being downloaded right now. Try again in a minute.")
        # what the song has: the upload it was downloaded from, or (another version already on its way) the one before
        previous = row["previous_youtube_id"] if row["status"] != "downloaded" and has_file(row) else \
            row["youtube_id"] if row["status"] == "downloaded" else None
        self.db.run("UPDATE tracks SET monitored = 1, priority = MAX(priority, ?), requested_at = ? WHERE id = ?",
                    (REQUEST_PRIORITY, time.time(), tid))
        self.db.set_status(tid, "wanted", youtube_id=youtube_id, pinned=1, previous_youtube_id=previous, error=None,
                           attempts=0, next_attempt=None, blocked=0)
        who = self.db.one("SELECT name FROM users WHERE id = ?", (user_id,)) if user_id else None
        song = f"{', '.join(json.loads(row['artists'] or '[]'))} - {row['title']}"
        self.db.log("manual-pick", f"{who[0] if who else 'You'} chose another version of {song}: https://youtu.be/{youtube_id}", tid)
        self.wake.set()

    def _remove_replaced(self, old: Path, new: Path, tid: str) -> None:
        """The file a new version replaced, when it was somewhere else: deleted if Songarr made it (for
        this song) inside the music library. Anything else is left alone, and so is the new file under
        another name for the same place (a mapped drive and its network path)."""
        try:
            root = Path(self.db.setting("library_root") or "").resolve()
            if old.is_file() and not old.samefile(new) and old.resolve().is_relative_to(root) and tagging.read_spotify_id(old) == tid:
                old.unlink()
                self.db.log("deleted", f"Removed the version it replaced ({old})", tid)
        except OSError as e:
            log.info("couldn't remove the replaced file %s: %s", old, e)

    def ignore(self, tid: str, ignored: bool) -> None:
        if ignored:
            self.db.set_status(tid, "ignored")
        else:
            self.retry(tid)

    def request_track(self, user_id: int, track: dict, log: bool = True) -> dict:
        """A song asked for from the app: add it to the shared library and download it next."""
        self.db.upsert_tracks([track])
        tid = track["id"]
        self.db.run("INSERT OR IGNORE INTO requests(user_id, track_id, created) VALUES (?, ?, ?)", (user_id, tid, time.time()))
        self.db.add_source(tid, f"req:{user_id}")
        # ahead of the backlog, and of earlier requests: the newest request downloads first
        self.db.run("UPDATE tracks SET monitored = 1, priority = MAX(priority, ?), requested_at = ? WHERE id = ?",
                    (REQUEST_PRIORITY, time.time(), tid))
        row = self.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))
        if row["status"] in ("failed", "ignored") or (row["status"] == "failed" and row["next_attempt"] is None):
            self.db.set_status(tid, "wanted", attempts=0, next_attempt=None, error=None)
        if log:
            name = self.db.one("SELECT name FROM users WHERE id = ?", (user_id,))
            self.db.log("requested", f"{name[0] if name else 'Someone'} requested {', '.join(track['artists'])} - {track['title']}", tid)
        self.wake.set()
        return track_dict(self.db.one("SELECT * FROM tracks WHERE id = ?", (tid,)))

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(v) for v in self.active.values()]

    def throttle_info(self) -> dict:
        per_hour = int(self.db.setting("max_per_hour") or 250)
        return {"max_per_hour": per_hour, "rate_factor": round(self.rate_factor, 2),
                "effective_per_hour": int(per_hour * self.rate_factor), "blocks": self.blocks,
                "cooldown_until": self.cooldown_until if self.cooldown_until > time.time() else None}


def _describe(c: Candidate) -> str:
    return f"https://youtu.be/{c.id}, match {c.score:.2f}"
