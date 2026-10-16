"""Smart Shuffle: while a playlist (or Liked Songs) plays shuffled, one song in three is one this
profile is likely to love next, picked from what the server can tell about everyone's listening.

Each song on the server is scored for the profile:
- fit: how much its artist (and its era) belongs in the list being played;
- taste: how much the profile plays, finishes and likes that artist (recent listening counts most);
- family: how much the people with the most similar taste played it to the end or liked it;
- discovery: artists Deezer says are like the profile's favourites (recommend.py);
- on trial (below): a little extra, so new songs get heard;
less what the profile skips (that song, and that artist). The best are drawn with some chance in it,
no more than two by one artist, so each shuffle differs.

Songs on trial: so suggestions aren't only what's already on the server, the made-for-you songs of the
profiles using Smart Shuffle are downloaded ahead into a folder of their own inside the music folder
(TRIAL_FOLDER, which media servers are told to skip). A song stays for good, moved into the music folder,
once anyone likes it, puts it in a playlist or requests it, or once it has been played to the end
KEEP_PLAYS times, by KEEP_PEOPLE people or more, more often than skipped. Otherwise it goes after
TRIAL_DAYS days (and isn't put on trial again).
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from . import discover, library
from .db import track_dict
from .matching import norm

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

TRIAL_FOLDER = "Songarr Suggestions"
READY = 10  # songs on trial kept ready (downloaded, not yet heard) for each profile using Smart Shuffle
ACTIVE_FOR = 14 * 86400  # a profile that used Smart Shuffle this recently gets songs on trial
PER_DAY = 40  # new songs put on trial a day, for everyone together (YouTube's patience)
KEEP_PLAYS, KEEP_PEOPLE = 4, 2
TRIAL_DAYS = 30
STUCK_DAYS = 3  # one that still isn't downloaded after this long is let go
TRIAL_PRIORITY = 5  # downloaded after requests (10), before the rest (0)
CHECK_EVERY = 10 * 60
SKIP_UNDER_MS = 30_000  # a song left sooner than this is a skip (the app says so: POST /skips)
_KEEPERS = {"like": "liked", "up": "added to a playlist", "req": "requested"}


def trial_root(library_root: str | Path) -> Path:
    """The folder songs on trial are downloaded to, with the files that keep Plex and Jellyfin out."""
    root = Path(library_root) / TRIAL_FOLDER
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name, text in ((".plexignore", "*\n"), (".ignore", "")):
            if not (root / name).exists():
                (root / name).write_text(text, encoding="utf-8")
    except OSError:
        pass  # the music folder is away for now: tried again with the next download
    return root


def _year(date: str | None) -> int | None:
    try:
        return int((date or "")[:4]) or None
    except ValueError:
        return None


class SmartShuffle:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()

    # -- what's used ----------------------------------------------------------------------------

    def used(self, user_id: int) -> None:
        """[user_id] shuffled with Smart Shuffle: keep songs on trial ready for them."""
        self.db.run("INSERT OR REPLACE INTO smart_shuffle(user_id, used) VALUES (?, ?)", (user_id, time.time()))
        self.wake.set()

    def skipped(self, user_id: int, track_id: str, ms_played: int) -> None:
        self.db.run("INSERT INTO skips(user_id, track_id, at, ms_played) VALUES (?, ?, ?, ?)",
                    (user_id, track_id, time.time(), ms_played))

    # -- picking --------------------------------------------------------------------------------

    def suggest(self, user_id: int, list_ids: list[str], n: int, exclude: set[str] | frozenset = frozenset(),
                rng: random.Random | None = None) -> list[tuple[str, str]]:
        """Up to [n] songs for [user_id] to hear among [list_ids] (the list being shuffled), best first
        with some chance in it: (track id, why)."""
        rng = rng or random.Random()
        now = time.time()
        rows = {r["id"]: r for r in self.db.q("""SELECT id, title, artists, album, release_date, trial FROM tracks
                                                WHERE status = 'downloaded' AND file_path IS NOT NULL""")}
        in_list = set(list_ids)
        listed = [rows[i] for i in list_ids if i in rows]

        def artist(r) -> str:
            arts = json.loads(r["artists"] or "[]")
            return norm(arts[0]) if arts else ""

        names = {}
        for r in rows.values():
            arts = json.loads(r["artists"] or "[]")
            if arts:
                names.setdefault(norm(arts[0]), arts[0])

        # the list: its artists, and its era
        list_artists = Counter(artist(r) for r in listed if artist(r))
        top_share = max(list_artists.values(), default=1)
        years = sorted(y for r in listed if (y := _year(r["release_date"])))
        median = years[len(years) // 2] if years else None

        # the profile: taste, songs it has (or recently heard), what it skips
        taste = self.svc.recommender.taste(user_id)[0]
        top_taste = max(taste.values(), default=1.0) or 1.0
        liked = {r[0] for r in self.db.q("SELECT track_id FROM track_sources WHERE source = ?", (f"like:{user_id}",))}
        recent = {r[0] for r in self.db.q("SELECT track_id FROM plays WHERE user_id = ? AND played_at > ?",
                                          (user_id, now - 3 * 86400))}
        heard = {r[0] for r in self.db.q("SELECT DISTINCT track_id FROM plays WHERE user_id = ?", (user_id,))}
        skipped_songs = {r[0] for r in self.db.q("SELECT DISTINCT track_id FROM skips WHERE user_id = ? AND at > ?",
                                                 (user_id, now - 180 * 86400))}
        artist_skips: Counter = Counter()
        for (a,) in self.db.q("""SELECT t.artists FROM skips s JOIN tracks t ON t.id = s.track_id
                                 WHERE s.user_id = ? AND s.at > ?""", (user_id, now - 90 * 86400)):
            if arts := json.loads(a or "[]"):
                artist_skips[norm(arts[0])] += 1
        artist_finishes: Counter = Counter()
        for (a,) in self.db.q("""SELECT t.artists FROM plays p JOIN tracks t ON t.id = p.track_id
                                 WHERE p.user_id = ? AND p.completed = 1 AND p.played_at > ?""", (user_id, now - 90 * 86400)):
            if arts := json.loads(a or "[]"):
                artist_finishes[norm(arts[0])] += 1

        # the family: how alike each other profile's taste is, and what they finish and like
        similar: dict[int, float] = {}
        for (other,) in self.db.q("SELECT id FROM users WHERE id != ?", (user_id,)):
            similar[other] = _cosine(taste, self.svc.recommender.taste(other)[0])
        loved: dict[str, float] = defaultdict(float)
        best_by: dict[str, tuple[float, int]] = {}
        for other, sim in similar.items():
            if sim <= 0:
                continue
            counts: Counter = Counter(r[0] for r in self.db.q("SELECT track_id FROM plays WHERE user_id = ? AND completed = 1",
                                                              (other,)))
            for r in self.db.q("SELECT track_id FROM track_sources WHERE source = ?", (f"like:{other}",)):
                counts[r[0]] += 2
            for tid, c in counts.items():
                part = sim * min(c, 3) / 3
                loved[tid] += part
                if part > best_by.get(tid, (0.0, 0))[0]:
                    best_by[tid] = (part, other)
        people = {r[0]: r[1] for r in self.db.q("SELECT id, name FROM users")}

        # artists like the profile's favourites (Deezer, via the made-for-you picks)
        related: dict[str, str] = {}
        for a in (self.svc.recommender.get(user_id) or {}).get("artists", []):
            if k := norm(a.get("name") or ""):
                related[k] = a.get("reason") or f"Like {a['name']}"

        scored = []
        for tid, r in rows.items():
            if tid in in_list or tid in exclude or tid in liked or tid in recent or tid in skipped_songs:
                continue
            a = artist(r)
            fit = list_artists.get(a, 0) / top_share
            year = _year(r["release_date"])
            if median and year:
                fit += 0.3 * math.exp(-abs(year - median) / 8)
            own = taste.get(a, 0.0) / top_taste
            family = min(loved.get(tid, 0.0), 1.0)
            discovery = 0.5 if a in related else 0.0
            trial = 0.4 if r["trial"] == 1 and tid not in heard else 0.0
            skips = artist_skips.get(a, 0)
            penalty = skips / (skips + artist_finishes.get(a, 0) + 1)
            score = 1.5 * fit + own + 0.8 * family + discovery + trial - 1.2 * penalty
            parts = {"fit": 1.5 * fit, "own": own, "family": 0.8 * family, "discovery": discovery, "trial": trial}
            scored.append((score, tid, a, max(parts, key=parts.get)))
        scored.sort(reverse=True)

        picks: list[tuple[str, str]] = []
        per_artist: Counter = Counter()
        pool = scored[:max(60, 4 * n)]
        while pool and len(picks) < n:
            weights = [math.exp(s / 0.35) for s, *_ in pool]
            i = rng.choices(range(len(pool)), weights=weights)[0]
            score, tid, a, why = pool.pop(i)
            if a and per_artist[a] >= 2:
                continue
            per_artist[a] += 1
            name = names.get(a, "")
            if why == "fit":
                reason = f"More from {name}" if name and a in list_artists else "Fits this list"
            elif why == "own":
                reason = f"Because you like {name}" if name else "New for you"
            elif why == "family":
                reason = f"Loved by {people.get(best_by.get(tid, (0.0, 0))[1], 'your family')}"
            else:  # discovery, on trial
                reason = related.get(a, "New for you") if why == "discovery" else "New for you"
            picks.append((tid, reason))
        return picks

    # -- songs on trial ---------------------------------------------------------------------------

    def run(self) -> None:
        next_check = time.time() + 60
        while not self.svc.stop_event.is_set():
            woken = self.wake.wait(timeout=60)
            self.wake.clear()
            if self.svc.stop_event.is_set():
                break
            if not woken and time.time() < next_check:
                continue
            next_check = time.time() + CHECK_EVERY
            try:
                self.judge()
                self.top_up()
            except Exception:  # never let one bad pass stop the loop
                log.exception("songs on trial")
            finally:
                self.db.release()

    def keeps(self, track_id: str) -> str | None:
        """Why a song on trial stays for good (None: it doesn't, yet)."""
        if row := self.db.one("""SELECT source FROM track_sources WHERE track_id = ?
                                 AND (source LIKE 'like:%' OR source LIKE 'up:%' OR source LIKE 'req:%') LIMIT 1""", (track_id,)):
            kind, _, who = row[0].partition(":")
            if kind == "up":
                owner = self.db.one("SELECT u.name FROM user_playlists p JOIN users u ON u.id = p.user_id WHERE p.id = ?", (who,))
            else:
                owner = self.db.one("SELECT name FROM users WHERE id = ?", (int(who) if who.isdigit() else -1,))
            return f"{_KEEPERS[kind]} by {owner[0] if owner else 'someone'}"
        finished = [r[0] for r in self.db.q("SELECT user_id FROM plays WHERE track_id = ? AND completed = 1", (track_id,))]
        skips = self.db.one("SELECT COUNT(*) FROM skips WHERE track_id = ?", (track_id,))[0]
        if len(finished) >= KEEP_PLAYS and len(set(finished)) >= KEEP_PEOPLE and len(finished) > skips:
            return f"played to the end {len(finished)} times by {len(set(finished))} people"
        return None

    def judge(self) -> None:
        """Keep the songs on trial that have earned it; let go of the ones whose time is up."""
        root = Path(self.db.setting("library_root") or "")
        now = time.time()
        for row in self.db.q("SELECT * FROM tracks WHERE trial = 1"):
            t = track_dict(row)
            path = Path(t["file_path"]) if t["file_path"] else None
            if t["status"] == "downloaded" and path and not path.is_relative_to(root / TRIAL_FOLDER):
                self._kept(t, "it was in the music folder already", move=False)
            elif why := self.keeps(t["id"]):
                self._kept(t, why)
            elif now - (t["trial_since"] or now) > (TRIAL_DAYS if t["status"] == "downloaded" else STUCK_DAYS) * 86400:
                self._drop(t)

    def _kept(self, t: dict, why: str, move: bool = True) -> None:
        path = Path(t["file_path"]) if t["file_path"] else None
        fields = {}
        if move and t["status"] == "downloaded" and path and path.is_file():
            dest = library.target_path(self.db.setting("library_root"), t, path.suffix.lstrip("."))
            final = library.place(path, dest)
            fields = {"file_path": str(final), "file_size": final.stat().st_size}
            self._tidy(path.parent)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.run(f"UPDATE tracks SET trial = 0, trial_since = NULL{', ' + cols if cols else ''} WHERE id = ?",
                    (*fields.values(), t["id"]))
        self.db.run("DELETE FROM track_sources WHERE track_id = ? AND source LIKE 'trial:%'", (t["id"],))
        self.db.log("kept", f"{', '.join(t['artists'])} - {t['title']} stays for good: {why}", t["id"])
        self.svc.playlists_dirty = True

    def _drop(self, t: dict) -> None:
        path = Path(t["file_path"]) if t["file_path"] else None
        root = Path(self.db.setting("library_root") or "") / TRIAL_FOLDER
        if path and path.is_relative_to(root):
            path.unlink(missing_ok=True)
            self._tidy(path.parent)
        self.db.set_status(t["id"], "wanted", file_path=None, file_size=None, monitored=0, priority=0, trial=2, trial_since=None)
        self.db.run("DELETE FROM track_sources WHERE track_id = ? AND source LIKE 'trial:%'", (t["id"],))
        self.db.log("trial", f"{', '.join(t['artists'])} - {t['title']}: its trial ended, not kept", t["id"])

    def _tidy(self, folder: Path) -> None:
        """Empty album and artist folders left in the trial folder."""
        root = Path(self.db.setting("library_root") or "") / TRIAL_FOLDER
        for f in (folder, folder.parent):
            if f == root or not f.is_relative_to(root):
                break
            try:
                f.rmdir()
            except OSError:
                break

    def top_up(self) -> None:
        """Download made-for-you songs to try out, for the profiles using Smart Shuffle."""
        now = time.time()
        budget = PER_DAY - self.db.one("SELECT COUNT(*) FROM tracks WHERE trial_since > ?", (now - 86400,))[0]
        active = [r[0] for r in self.db.q("SELECT user_id FROM smart_shuffle WHERE used > ?", (now - ACTIVE_FOR,))]
        if budget <= 0 or not active:
            return
        known: set[str] = set()
        isrcs: set[str] = set()
        for r in self.db.q("SELECT title, artists, isrc FROM tracks"):
            known.add(discover.song_key(json.loads(r["artists"] or "[]"), r["title"]))
            if r["isrc"]:
                isrcs.add(r["isrc"])
        added = 0
        for uid in active:
            ready = self.db.one("""SELECT COUNT(*) FROM tracks WHERE trial = 1
                                   AND id IN (SELECT track_id FROM track_sources WHERE source = ?)
                                   AND id NOT IN (SELECT track_id FROM plays WHERE user_id = ?)
                                   AND id NOT IN (SELECT track_id FROM skips WHERE user_id = ?)""",
                                (f"trial:{uid}", uid, uid))[0]
            for song in (self.svc.recommender.get(uid) or {}).get("songs", []):
                if ready >= READY or budget <= 0:
                    break
                if song.get("source") != "deezer" or discover.song_key(song["artists"], song["title"]) in known:
                    continue
                try:
                    track = discover.full_track(int(song["id"]))
                except (OSError, ValueError, KeyError, RuntimeError) as e:
                    log.info("couldn't look up %s for a trial: %s", song.get("title"), e)
                    continue
                key = discover.song_key(track["artists"], track["title"])
                if key in known or (track.get("isrc") and track["isrc"] in isrcs):
                    continue
                known.add(key)
                track.setdefault("added_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                self.db.upsert_tracks([track])
                self.db.add_source(track["id"], f"trial:{uid}")
                self.db.run("""UPDATE tracks SET trial = 1, trial_since = ?, monitored = 1, priority = MAX(priority, ?)
                               WHERE id = ? AND trial = 0""", (now, TRIAL_PRIORITY, track["id"]))
                ready += 1
                budget -= 1
                added += 1
        if added:
            log.info("put %d song%s on trial", added, "" if added == 1 else "s")
            self.svc.wake.set()


def _cosine(a: Counter, b: Counter) -> float:
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0

