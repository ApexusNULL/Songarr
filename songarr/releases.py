"""Following artists, and hearing about their new releases.

Follow an artist in the app (their page → Follow). A few times a day Songarr asks Deezer for every
followed artist's releases. A release it hasn't seen before that came out in the last two weeks
(or is about to) is news: each follower gets a notification in the app (the bell on Home) and, when
Firebase is set up, on their phone, and it shows on Home under "New releases" with the other
recent releases of the artists they follow. Tapping one opens the album, ready to add.

The first time an artist is looked at, everything already out is just remembered, so following
someone with a long discography doesn't set off a pile of notifications.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, Callable

from . import discover
from .matching import norm

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

FIRST_CHECK = 5 * 60  # after starting
CHECK_EVERY = 6 * 3600
NEWS_DAYS = 14  # a release found this long after it came out is old news (a back-catalogue upload)
SHELF_DAYS = 60  # Home's "New releases": out within this many days
KEEP_NOTIFICATIONS = 200  # per person
TYPE_WORDS = {"single": "single", "ep": "EP", "compile": "compilation", "album": "album"}


class FollowError(LookupError):
    pass


class Releases:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()
        self.state: dict = {"running": False, "checked": None, "found": 0, "error": None}
        self._lock = threading.Lock()
        # swapped out in tests
        self.fetch: Callable[[int, str], list[dict]] = discover.artist_releases
        self.find: Callable[[str], dict | None] = discover.find_artist
        self.today: Callable[[], date] = date.today
        self.in_background = True  # tests look at a newly followed artist's releases straight away

    # -- following ----------------------------------------------------------------------------------

    def follow(self, user_id: int, name: str, deezer_id: int | None = None, image_url: str | None = None) -> dict:
        """Follow an artist (by Deezer id, or found by name). Returns the artist."""
        if deezer_id is None or not name:
            found = self.find(name)
            if found is None:
                raise FollowError(f"Couldn't find {name} in the music catalogue.")
            deezer_id, name, image_url = found["deezer_id"], found["name"], found.get("thumb_url") or found.get("image_url")
        elif image_url is None:  # their photo, for the list of artists you follow (the artist page just looked them up)
            try:
                found = self.find(name)
                if found and found["deezer_id"] == deezer_id:
                    image_url = found.get("thumb_url") or found.get("image_url")
            except (OSError, RuntimeError, ValueError):
                pass
        known = self.db.one("SELECT 1 FROM artists WHERE deezer_id = ?", (deezer_id,))
        self.db.run("""INSERT INTO artists(deezer_id, name, image_url) VALUES (?, ?, ?)
                       ON CONFLICT(deezer_id) DO UPDATE SET name = excluded.name,
                       image_url = COALESCE(excluded.image_url, artists.image_url)""", (deezer_id, name, image_url))
        self.db.run("INSERT OR IGNORE INTO artist_follows(user_id, deezer_id, followed_at) VALUES (?, ?, ?)",
                    (user_id, deezer_id, time.time()))
        if not known:  # look at their releases now, so Home can show the latest ones straight away
            if self.in_background:
                threading.Thread(target=self._check_quietly, args=(deezer_id,), name="releases-first", daemon=True).start()
            else:
                self.check_artist(deezer_id)
        return {"deezer_id": deezer_id, "name": name, "image_url": image_url, "following": True}

    def unfollow(self, user_id: int, deezer_id: int) -> None:
        self.db.run("DELETE FROM artist_follows WHERE user_id = ? AND deezer_id = ?", (user_id, deezer_id))

    def following(self, user_id: int) -> list[dict]:
        rows = self.db.q("""SELECT a.deezer_id, a.name, a.image_url, f.followed_at,
                              (SELECT json_object('name', r.title, 'release_date', r.release_date, 'type', r.record_type)
                               FROM artist_releases r WHERE r.deezer_artist_id = a.deezer_id
                               ORDER BY r.release_date DESC LIMIT 1) AS latest
                            FROM artist_follows f JOIN artists a ON a.deezer_id = f.deezer_id
                            WHERE f.user_id = ? ORDER BY a.name COLLATE NOCASE""", (user_id,))
        return [{"deezer_id": r["deezer_id"], "name": r["name"], "image_url": r["image_url"], "followed_at": r["followed_at"],
                 "latest": json.loads(r["latest"]) if r["latest"] else None} for r in rows]

    def follows(self, user_id: int, name: str, deezer_id: int | None = None) -> int | None:
        """The Deezer id of this artist if [user_id] follows them (by id, or by name)."""
        if deezer_id is not None:
            r = self.db.one("SELECT deezer_id FROM artist_follows WHERE user_id = ? AND deezer_id = ?", (user_id, deezer_id))
            return r[0] if r else None
        for r in self.db.q("""SELECT a.deezer_id, a.name FROM artist_follows f JOIN artists a ON a.deezer_id = f.deezer_id
                              WHERE f.user_id = ?""", (user_id,)):
            if norm(r["name"]) == norm(name):
                return r["deezer_id"]
        return None

    # -- what's out ---------------------------------------------------------------------------------

    def recent(self, user_id: int, limit: int = 20) -> list[dict]:
        """Releases from the artists [user_id] follows that came out in the last SHELF_DAYS, newest first."""
        since = (self.today() - timedelta(days=SHELF_DAYS)).isoformat()
        rows = self.db.q("""SELECT r.*, a.name AS artist FROM artist_releases r
                            JOIN artist_follows f ON f.deezer_id = r.deezer_artist_id AND f.user_id = ?
                            JOIN artists a ON a.deezer_id = r.deezer_artist_id
                            WHERE r.release_date >= ? ORDER BY r.release_date DESC, r.first_seen DESC LIMIT ?""",
                         (user_id, since, limit))
        return [self.album_out(r) for r in rows]

    @staticmethod
    def album_out(r) -> dict:
        """A release as the app's catalogue albums look (it opens the album page)."""
        return {"source": "deezer", "id": r["album_id"], "name": r["title"], "artists": [r["artist"]],
                "type": r["record_type"] or "album", "count": None, "explicit": bool(r["explicit"]),
                "cover_url": r["cover_url"], "thumb_url": r["thumb_url"], "release_date": r["release_date"]}

    # -- checking -----------------------------------------------------------------------------------

    def check(self) -> int:
        """Look at every followed artist's releases. Returns how many new releases were found."""
        with self._lock:
            self.state.update(running=True, error=None)
            found = 0
            try:
                for r in self.db.q("SELECT deezer_id FROM artists WHERE deezer_id IN (SELECT deezer_id FROM artist_follows)"):
                    if self.svc.stop_event.is_set():
                        break
                    try:
                        found += len(self.check_artist(r[0]))
                    except (OSError, RuntimeError, ValueError, KeyError) as e:
                        self.state["error"] = str(e)
                        log.info("couldn't check releases of artist %s: %s", r[0], e)
                self._tidy()
            finally:
                self.state.update(running=False, checked=time.time(), found=found)
        return found

    def check_artist(self, deezer_id: int) -> list[dict]:
        """New releases of one artist (already announced to their followers)."""
        artist = self.db.one("SELECT name, checked FROM artists WHERE deezer_id = ?", (deezer_id,))
        if artist is None:
            return []
        releases = self.fetch(deezer_id, artist["name"])
        known = {r[0] for r in self.db.q("SELECT album_id FROM artist_releases WHERE deezer_artist_id = ?", (deezer_id,))}
        first_look = artist["checked"] is None
        cutoff = (self.today() - timedelta(days=NEWS_DAYS)).isoformat()
        news = []
        now = time.time()
        with self.db.tx() as c:
            for a in releases:
                if a["id"] in known:
                    continue
                c.execute("""INSERT OR IGNORE INTO artist_releases(album_id, deezer_artist_id, title, release_date, record_type,
                             cover_url, thumb_url, explicit, first_seen) VALUES (?,?,?,?,?,?,?,?,?)""",
                          (a["id"], deezer_id, a["name"], a["release_date"], a["type"], a["cover_url"], a["thumb_url"],
                           int(a["explicit"]), now))
                if not first_look and (a["release_date"] or "9999") >= cutoff:
                    news.append(a)
            c.execute("UPDATE artists SET checked = ? WHERE deezer_id = ?", (now, deezer_id))
        for a in news:
            self._announce(deezer_id, artist["name"], a)
        return news

    def _announce(self, deezer_id: int, artist: str, a: dict) -> None:
        followers = [r[0] for r in self.db.q("SELECT user_id FROM artist_follows WHERE deezer_id = ?", (deezer_id,))]
        if not followers:
            return
        title = f"New {TYPE_WORDS.get(a['type'], 'release')} from {artist}"
        album = self.album_out({"album_id": a["id"], "title": a["name"], "artist": artist, "record_type": a["type"],
                                "explicit": a["explicit"], "cover_url": a["cover_url"], "thumb_url": a["thumb_url"],
                                "release_date": a["release_date"]})
        now = time.time()
        with self.db.tx() as c:
            c.executemany("INSERT INTO notifications(user_id, kind, title, body, data, created) VALUES (?, 'release', ?, ?, ?, ?)",
                          [(uid, title, a["name"], json.dumps({"album": album}), now) for uid in followers])
        self.db.log("release", f"{title}: {a['name']} ({a['release_date'] or 'date unknown'})")
        self.svc.push.send_async(followers, title, a["name"], {"type": "new_release", "album": str(a["id"]), "name": a["name"],
                                                               "artist": artist, "kind": a["type"], "thumb": a["thumb_url"] or ""},
                                 channel="releases")

    def _check_quietly(self, deezer_id: int) -> None:
        try:
            self.check_artist(deezer_id)
        except Exception as e:  # offline: the next check catches up
            log.info("couldn't look at releases of artist %s yet: %s", deezer_id, e)
        finally:
            self.db.release()

    def _tidy(self) -> None:
        """Forget artists nobody follows any more, and keep each person's last notifications."""
        self.db.run("DELETE FROM artist_releases WHERE deezer_artist_id NOT IN (SELECT deezer_id FROM artist_follows)")
        self.db.run("DELETE FROM artists WHERE deezer_id NOT IN (SELECT deezer_id FROM artist_follows)")
        self.db.run("""DELETE FROM notifications WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER
                       (PARTITION BY user_id ORDER BY created DESC) AS n FROM notifications) WHERE n > ?)""", (KEEP_NOTIFICATIONS,))

    # -- notifications ------------------------------------------------------------------------------

    def notifications(self, user_id: int, limit: int = 50) -> dict:
        rows = self.db.q("SELECT * FROM notifications WHERE user_id = ? ORDER BY created DESC, id DESC LIMIT ?", (user_id, limit))
        unread = self.db.one("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0", (user_id,))[0]
        return {"notifications": [{"id": r["id"], "kind": r["kind"], "title": r["title"], "body": r["body"],
                                   "created": r["created"], "read": bool(r["read"]), **json.loads(r["data"] or "{}")}
                                  for r in rows], "unread": unread}

    def mark_read(self, user_id: int, ids: list[int] | None = None) -> None:
        if ids:
            self.db.run(f"UPDATE notifications SET read = 1 WHERE user_id = ? AND id IN ({','.join('?' * len(ids))})",
                        (user_id, *ids))
        else:
            self.db.run("UPDATE notifications SET read = 1 WHERE user_id = ?", (user_id,))

    def unread(self, user_id: int) -> int:
        return self.db.one("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0", (user_id,))[0]

    # -- the background loop ------------------------------------------------------------------------

    def run(self) -> None:
        next_check = time.time() + FIRST_CHECK
        while not self.svc.stop_event.is_set():
            self.wake.wait(timeout=60)
            self.wake.clear()
            if self.svc.stop_event.is_set():
                break
            if time.time() >= next_check:
                next_check = time.time() + CHECK_EVERY
                try:
                    self.check()
                except Exception:
                    log.exception("release check failed")
                finally:
                    self.db.release()
