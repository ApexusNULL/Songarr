"""Each song's genre and era (the year it first came out), so playlists and Liked Songs can be sorted
and grouped by them: "80s Rap", "00s Pop". Looked up once per song, in the background, about a song
a second (people's own songs first):

- genre: Deezer's genre for the album the song is on (Pop, Rap, Rock, Dance, R&B...), found by the
  song's recording code (ISRC), or else a search by artist and title. A song still without one takes
  the genre most of its artist's other songs have.
- year: when the recording first came out, from MusicBrainz (by ISRC, or else a search by artist and
  title), so a 2011 remaster of a 1975 song counts as 1975. Failing that, the earliest release date of
  the same song (same artist and title) anywhere in the library.
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
from typing import TYPE_CHECKING

from . import __version__, discover
from .matching import norm, strip_title, title_similarity

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

MB = "https://musicbrainz.org/ws/2"
AGENT = f"Songarr/{__version__} ( https://github.com/ApexusNULL/Songarr )"  # (MusicBrainz asks every app to say who it is)
MB_SPACING = 1.1  # seconds: MusicBrainz allows one request a second
STARTUP_DELAY = 120  # seconds before the first look after starting; tests raise it
BATCH = 40
OFFLINE_WAIT = 600  # after a lookup failed for want of the internet
# Deezer's genre names, as people say them
NAMES = {"Rap/Hip Hop": "Rap", "Films/Games": "Soundtracks", "Electro": "Electronic", "Latin Music": "Latin",
         "Asian Music": "Asian", "African Music": "African", "Brazilian Music": "Brazilian", "Indian Music": "Indian",
         "Traditional Mexicano": "Mexican"}
_YEAR = re.compile(r"^(\d{4})")


class Offline(Exception):
    pass


def _year(date: str | None) -> int | None:
    m = _YEAR.match(date or "")
    return int(m.group(1)) if m and 1900 <= int(m.group(1)) <= 2100 else None


class Genres:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()
        self.albums: dict[int, str | None] = {}  # Deezer album -> its genre
        self._mb_last = 0.0

    # -- lookups ------------------------------------------------------------------------------------

    def _deezer(self, path: str) -> dict | None:
        try:
            return discover._get(path, 0)  # (not kept: thousands of songs would fill the catalogue's cache)
        except RuntimeError:
            return None  # Deezer has no such thing
        except (OSError, ValueError) as e:
            raise Offline(str(e)) from None

    def _album_genre(self, album_id: int) -> str | None:
        if album_id not in self.albums:
            a = self._deezer(f"/album/{album_id}") or {}
            listed = {g.get("id"): g.get("name") for g in (a.get("genres") or {}).get("data", []) if g.get("name")}
            name = listed.get(a.get("genre_id")) or next(iter(listed.values()), None)
            if name is None and (a.get("genre_id") or 0) > 0:
                name = (self._deezer(f"/genre/{a['genre_id']}") or {}).get("name")
            self.albums[album_id] = NAMES.get(name, name) if name else None
        return self.albums[album_id]

    def genre(self, t: dict) -> str | None:
        """Deezer's genre for the song's album."""
        found = None
        if t["id"].startswith("dz") and t["id"][2:].isdigit():
            found = self._deezer(f"/track/{t['id'][2:]}")
        if found is None and t.get("isrc"):
            found = self._deezer(f"/track/isrc:{urllib.parse.quote(t['isrc'])}")
        if found is None and t.get("artists") and t.get("title"):
            found = self._search(t["artists"][0], t["title"])
        album = ((found or {}).get("album") or {}).get("id")
        return self._album_genre(int(album)) if album else None

    def _search(self, artist: str, title: str) -> dict | None:
        """The song in Deezer's catalogue by artist and title: by that artist (not a cover), the title
        close, and not a live version unless it is one."""
        q = urllib.parse.quote(f"{artist} {strip_title(title)}")
        hits = (self._deezer(f"/search/track?q={q}&limit=10") or {}).get("data") or []
        live = "live" in norm(title).split()
        ok = [h for h in hits if norm((h.get("artist") or {}).get("name") or "") == norm(artist)
              and title_similarity(h.get("title") or "", title) >= 0.8]
        return next((h for h in ok if live or "live" not in norm(h.get("title") or "").split()), ok[0] if ok else None)

    def _mb(self, path: str) -> dict | None:
        for attempt in range(4):
            time.sleep(max(0.0, self._mb_last + MB_SPACING - time.time()))
            self._mb_last = time.time()
            req = urllib.request.Request(f"{MB}{path}", headers={"User-Agent": AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                wait = e.headers.get("Retry-After") or ""
                e.close()
                if e.code in (400, 404):
                    return None  # MusicBrainz doesn't know it
                if e.code != 503 or attempt == 3:
                    raise Offline(f"MusicBrainz: HTTP {e.code}") from None
                # busy (it says so now and then): it says when to ask again
                time.sleep(min(30.0, float(wait)) if wait.isdigit() else 2.0 * (attempt + 1))
            except (OSError, ValueError) as e:
                raise Offline(str(e)) from None
        return None

    def first_year(self, t: dict) -> int | None:
        """When the recording first came out, by MusicBrainz."""
        if t.get("isrc"):
            data = self._mb(f"/isrc/{urllib.parse.quote(t['isrc'])}?fmt=json") or {}
            years = [y for r in data.get("recordings") or [] if (y := _year(r.get("first-release-date")))]
            if years:
                return min(years)
        if not (t.get("artists") and t.get("title")):
            return None
        title, artist = strip_title(t["title"]).replace('"', ""), t["artists"][0].replace('"', "")
        q = urllib.parse.quote(f'recording:"{title}" AND artist:"{artist}"')
        data = self._mb(f"/recording?query={q}&limit=5&fmt=json") or {}
        years = [y for r in data.get("recordings") or [] if (r.get("score") or 0) >= 95 and (y := _year(r.get("first-release-date")))]
        return min(years) if years else None

    # -- the background look ------------------------------------------------------------------------

    def check(self, limit: int = BATCH) -> int:
        """Look up the next songs that haven't been; returns how many. Raises Offline when there's no
        internet (or MusicBrainz wants a break)."""
        rows = self.db.q("""SELECT id, title, artists, isrc, release_date FROM tracks WHERE facts_checked IS NULL
                            ORDER BY monitored DESC, added_at DESC LIMIT ?""", (limit,))
        if not rows:
            return 0
        earliest: dict[str, int] = {}  # the same song anywhere in the library: its first release date
        for r in self.db.q("SELECT title, artists, release_date FROM tracks WHERE release_date IS NOT NULL"):
            y = _year(r["release_date"])
            if y:
                key = discover.song_key(json.loads(r["artists"] or "[]"), r["title"])
                earliest[key] = min(earliest.get(key, y), y)
        done = 0
        for r in rows:
            t = {"id": r["id"], "title": r["title"], "artists": json.loads(r["artists"] or "[]"), "isrc": r["isrc"]}
            genre, year = self.genre(t), self.first_year(t)
            known = earliest.get(discover.song_key(t["artists"], t["title"])) or _year(r["release_date"])
            year = min(y for y in (year, known) if y) if year or known else None
            self.db.run("UPDATE tracks SET genre = ?, year = ?, facts_checked = ? WHERE id = ?", (genre, year, time.time(), r["id"]))
            done += 1
        # still no genre: the one most of that artist's other songs have
        self.db.run("""UPDATE tracks SET genre = (
                           SELECT o.genre FROM tracks o WHERE o.genre IS NOT NULL
                           AND json_extract(o.artists, '$[0]') = json_extract(tracks.artists, '$[0]')
                           GROUP BY o.genre ORDER BY COUNT(*) DESC LIMIT 1)
                       WHERE genre IS NULL AND facts_checked IS NOT NULL""")
        return done

    def run(self) -> None:
        started = time.time()
        while not self.svc.stop_event.is_set():
            self.wake.wait(timeout=5 if time.time() - started < STARTUP_DELAY else 60)
            self.wake.clear()
            if self.svc.stop_event.is_set() or time.time() - started < STARTUP_DELAY:
                continue
            try:
                while not self.svc.stop_event.is_set() and self.check():
                    pass
            except Offline as e:
                log.info("genre and year lookups wait: %s", e)
                self.svc.stop_event.wait(OFFLINE_WAIT)
            except Exception:
                log.exception("genre and year lookups failed")
                self.svc.stop_event.wait(OFFLINE_WAIT)
            finally:
                self.db.release()
