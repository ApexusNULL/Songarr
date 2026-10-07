"""Made-for-you songs, artists and podcasts picked for each profile.

Taste comes from what the profile already loves: Liked Songs, playlists and recent plays,
weighted towards the artists heard most. Related artists and their popular songs come from
Deezer's public API; podcasts from the Apple Podcasts charts for the genres of the shows the
profile follows. Results are rebuilt in the background twice a day (and after a library import),
so opening the app never waits on outside services.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING

from . import discover, podcasts
from .matching import norm

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

STALE_AFTER = 12 * 3600
STARTUP_DELAY = 15  # seconds; tests turn background picking off by raising it
SONGS = 24
ARTISTS = 15
PODCASTS = 15
_ERRORS = (OSError, RuntimeError, ValueError, KeyError, podcasts.PodcastError)


class Recommender:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.db = svc.db
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()

    # -- reading --------------------------------------------------------------------

    def get(self, user_id: int) -> dict | None:
        """The latest picks for a profile (None until the first are ready). Stale ones are rebuilt."""
        row = self.db.one("SELECT data, computed FROM recommendations WHERE user_id = ?", (user_id,))
        if row is None or time.time() - row["computed"] > STALE_AFTER:
            self.schedule(user_id)
        return json.loads(row["data"]) if row else None

    def schedule(self, *user_ids: int) -> None:
        with self._lock:
            self._pending.update(user_ids)
        self._wake.set()

    def schedule_all(self) -> None:
        self.schedule(*(r[0] for r in self.db.q("SELECT id FROM users")))

    # -- background ---------------------------------------------------------------------

    def run(self) -> None:
        stop = self.svc.stop_event
        if stop.wait(STARTUP_DELAY):  # let startup settle first
            return
        stale = time.time() - STALE_AFTER
        self.schedule(*(r[0] for r in self.db.q(
            "SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM recommendations WHERE computed > ?)", (stale,))))
        while not stop.is_set():
            self._wake.wait(timeout=3600)
            self._wake.clear()
            if stop.is_set():
                break
            with self._lock:
                todo = sorted(self._pending)
                self._pending.clear()
            if not todo:  # hourly check for anything that went stale
                todo = [r[0] for r in self.db.q("""SELECT u.id FROM users u LEFT JOIN recommendations r ON r.user_id = u.id
                                                   WHERE r.computed IS NULL OR r.computed < ?""", (time.time() - STALE_AFTER,))]
            for uid in todo:
                if stop.is_set():
                    break
                try:
                    self.refresh(uid)
                except Exception:
                    log.exception("couldn't build recommendations for profile %s", uid)
        self.db.release()

    def refresh(self, user_id: int) -> dict:
        if not self.db.one("SELECT 1 FROM users WHERE id = ?", (user_id,)):
            return {}
        started = time.time()
        data = self.compute(user_id)
        self.db.run("INSERT OR REPLACE INTO recommendations(user_id, data, computed) VALUES (?,?,?)",
                    (user_id, json.dumps(data), time.time()))
        log.info("recommendations for profile %s: %d songs, %d artists, %d podcasts (%.1fs)", user_id,
                 len(data["songs"]), len(data["artists"]), len(data["podcasts"]), time.time() - started)
        return data

    # -- taste -------------------------------------------------------------------------

    def taste(self, user_id: int) -> tuple[Counter, dict[str, str]]:
        """(normalised artist name -> weight, normalised name -> display name)."""
        weights: Counter = Counter()
        names: dict[str, str] = {}

        def add(artists_json: str, w: float) -> None:
            for i, a in enumerate(json.loads(artists_json or "[]")[:3]):
                if k := norm(a):
                    weights[k] += w if i == 0 else w * 0.5
                    names.setdefault(k, a)

        for (artists,) in self.db.q("""SELECT artists FROM tracks WHERE id IN
                                       (SELECT track_id FROM track_sources WHERE source = ?)""", (f"like:{user_id}",)):
            add(artists, 1.0)
        for (artists,) in self.db.q("""SELECT t.artists FROM tracks t JOIN track_sources s ON s.track_id = t.id
                                       WHERE s.source IN (SELECT 'up:' || id FROM user_playlists WHERE user_id = ?)""",
                                    (user_id,)):
            add(artists, 0.3)
        now = time.time()
        for artists, played_at in self.db.q("""SELECT t.artists, p.played_at FROM plays p JOIN tracks t ON t.id = p.track_id
                                               WHERE p.user_id = ? AND p.played_at > ?""", (user_id, now - 120 * 86400)):
            add(artists, 0.6 * 0.5 ** ((now - played_at) / (30 * 86400)))
        return weights, names

    # -- picking ---------------------------------------------------------------------------

    def compute(self, user_id: int) -> dict:
        weights, names = self.taste(user_id)
        ranked = [k for k, _ in weights.most_common()]
        rng = random.Random(f"{user_id}-{time.strftime('%Y-%m-%d')}")  # fresh picks daily, stable within a day
        found: dict[str, dict] = {}
        for key in ranked[:10]:
            if a := _quiet(discover.find_artist, names[key]):
                found[key] = a
        top_artists = [{"name": names[k], "image_url": found[k]["image_url"] if k in found else None,
                        "thumb_url": found[k]["thumb_url"] if k in found else None,
                        "deezer_id": found[k]["deezer_id"] if k in found else None} for k in ranked[:10]]

        # Artists like the ones you play most, that you don't already listen to a lot.
        scores: Counter = Counter()
        info: dict[str, dict] = {}
        because: dict[str, tuple[float, str]] = {}
        top_weight = weights[ranked[0]] if ranked else 1.0
        for key in ranked[:8]:
            if key not in found:
                continue
            seed = weights[key] / top_weight
            for rank, r in enumerate(_quiet(discover.related_artists, found[key]["deezer_id"], 15) or []):
                k = norm(r["name"])
                if not k or weights.get(k, 0) >= 1.0 or k in found:
                    continue
                part = seed * (1 - rank / 20)
                scores[k] += part
                info.setdefault(k, r)
                if part > because.get(k, (0.0, ""))[0]:
                    because[k] = (part, names[key])
        artists = [info[k] | {"reason": f"Because you like {because[k][1]}"} for k, _ in scores.most_common(ARTISTS)]
        if not artists:  # nothing to go on yet: what's popular
            artists = [a | {"reason": "Popular right now"} for a in _quiet(discover.chart_artists, ARTISTS) or []]

        songs = self._songs(user_id, weights, names, ranked, found, artists, rng)
        return {"songs": songs, "artists": artists, "top_artists": top_artists,
                "podcasts": self._podcasts(user_id, rng), "computed": time.time()}

    def _songs(self, user_id: int, weights: Counter, names: dict[str, str], ranked: list[str], found: dict[str, dict],
               artists: list[dict], rng: random.Random) -> list[dict]:
        by_key = {}
        for r in self.db.q("SELECT id, title, artists, status FROM tracks WHERE status != 'ignored'"):
            by_key.setdefault(discover.song_key(json.loads(r["artists"]), r["title"]), r)
        known = {r[0] for r in self.db.q("SELECT track_id FROM track_sources WHERE source = ?", (f"like:{user_id}",))}
        known |= {r[0] for r in self.db.q("SELECT track_id FROM plays WHERE user_id = ? AND played_at > ?",
                                          (user_id, time.time() - 90 * 86400))}

        def from_catalog(t: dict, reason: str) -> dict | None:
            row = by_key.get(discover.song_key(t["artists"], t["title"]))
            if row is not None and row["id"] in known:
                return None  # you already love it
            item = {k: t[k] for k in ("title", "artists", "album", "duration_ms", "explicit", "cover_url", "thumb_url")}
            return item | {"source": "deezer", "id": t["deezer_id"], "reason": reason}

        # Popular songs by artists you might like.
        related: list[dict] = []
        for a in artists[:8]:
            picks = [x for t in _quiet(discover.artist_top, a["deezer_id"], 5) or [] if (x := from_catalog(t, a["reason"]))]
            related += picks[:2]
        # Hits by your favourite artists that you haven't saved.
        favourites: list[dict] = []
        for key in ranked[:6]:
            if key in found:
                picks = [x for t in _quiet(discover.artist_top, found[key]["deezer_id"], 10) or []
                         if (x := from_catalog(t, f"More from {names[key]}"))]
                favourites += picks[:2]
        # Songs already on the server (someone else's, or from a playlist) by artists you like.
        top = set(ranked[:25])
        local = []
        for r in self.db.q("SELECT id, title, artists FROM tracks WHERE status = 'downloaded'"):
            arts = json.loads(r["artists"])
            if r["id"] not in known and arts and norm(arts[0]) in top:
                local.append({"source": "library", "id": r["id"], "title": r["title"], "artists": arts,
                              "reason": f"On your server · {names[norm(arts[0])]}", "w": weights[norm(arts[0])]})
        rng.shuffle(local)
        local.sort(key=lambda x: -x["w"] * rng.uniform(0.5, 1.5))
        for x in local:
            x.pop("w")
        if not (related or favourites or local):
            related = [x for t in _quiet(discover.genre_chart, 0, 30) or [] if (x := from_catalog(t, "Popular right now"))]
        rng.shuffle(related)
        rng.shuffle(favourites)

        out, seen = [], set()
        lists = [related, local, favourites]
        while len(out) < SONGS and any(lists):
            for lst in lists:
                while lst:
                    x = lst.pop(0)
                    key = discover.song_key(x["artists"], x["title"])
                    if key not in seen:
                        seen.add(key)
                        out.append(x)
                        break
        return out[:SONGS]

    def _podcasts(self, user_id: int, rng: random.Random) -> list[dict]:
        store = self.svc.podcasts
        followed = store.followed_ids(user_id)
        for pid in followed[:30]:  # so Home can show their new episodes
            _quiet(store.refresh, pid, 6 * 3600)
        genres: Counter = Counter()
        names: dict[str, str] = {}
        if followed:
            for r in self.db.q(f"SELECT genre, genre_ids FROM podcasts WHERE id IN ({','.join('?' * len(followed))})", followed):
                ids = json.loads(r["genre_ids"] or "[]")
                if ids:
                    genres[ids[0]] += 1
                    names.setdefault(ids[0], r["genre"] or "")
        charts = []
        for gid, _ in genres.most_common(3):
            chart = _quiet(podcasts.top, gid, 25) or []
            charts.append([p | {"reason": f"Popular in {names[gid] or p.get('genre') or 'podcasts'}"} for p in chart])
        if len(followed) < 3 or not charts:
            charts.append([p | {"reason": "Popular right now"} for p in _quiet(podcasts.top, None, 30) or []])
        out, seen = [], set(followed)
        while len(out) < PODCASTS and any(charts):
            for chart in charts:
                while chart:
                    p = chart.pop(0)
                    if p["id"] not in seen:
                        seen.add(p["id"])
                        out.append(p)
                        break
        if out:
            store.upsert(out)
        return out[:PODCASTS]


def _quiet(fn, *args):
    """Outside services fail sometimes; a missing shelf is better than no Home screen."""
    try:
        return fn(*args)
    except _ERRORS as e:
        log.info("%s%r failed: %s", fn.__name__, args, e)
        return None
