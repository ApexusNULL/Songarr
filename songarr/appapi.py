"""Songarr app API: the only part of Songarr meant to be reachable from the internet.

Put it behind HTTPS (Cloudflare Tunnel -> http://127.0.0.1:8486). It serves JSON and audio, and
one web page: the app's download page at /download (download_page.py; it can be switched off).
Anything else outside /api/v1/ answers an empty 404, and every endpoint except pairing needs a
device token (Authorization: Bearer <token>).

Library model: songs are shared by everyone; likes, playlists, plays and requests are per
profile. A profile's library = the songs it liked, put in its playlists or requested (Spotify libraries
come over from an Exportify file, see exportify.py). For older apps the Spotify fields are still sent,
empty: /me's spotify_accounts and on_spotify.
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import threading
import time
import urllib.error
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, app_updates, discover, download_page, podcasts
from .releases import FollowError
from . import jams
from .jams import Jam, JamError
from .db import track_dict
from .matching import TrackInfo, norm
from .service import Service
from .youtube import youtube_id
from .users import SignInError

log = logging.getLogger(__name__)

MIME = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".flac": "audio/flac",
        ".aac": "audio/aac", ".wav": "audio/wav", ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".apk": "application/vnd.android.package-archive", ".png": "image/png", ".webp": "image/webp"}
MAX_BODY = 64 * 1024
CHUNK = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class AppAPI:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.users = svc.users
        self._index: tuple[float, dict, dict] | None = None
        self._index_lock = threading.Lock()

    # -- scoping helpers ----------------------------------------------------------

    def library_sources_sql(self, uid: int) -> tuple[str, list]:
        """SQL (+params) selecting the track ids in a profile's library."""
        sql = """SELECT track_id FROM track_sources WHERE source IN (?, ?)
                 OR source IN (SELECT 'up:' || id FROM user_playlists WHERE user_id = ?)"""
        return sql, [f"req:{uid}", f"like:{uid}", uid]

    def liked_ids(self, uid: int) -> set[str]:
        return {r[0] for r in self.db.q("SELECT track_id FROM likes WHERE user_id = ?", (uid,))}

    def tracks_by_ids(self, ids: list[str], uid: int) -> list[dict]:
        if not ids:
            return []
        rows = {r["id"]: r for r in self.db.q(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        liked = self.liked_ids(uid)
        return [self.track_out(rows[i], liked) for i in ids if i in rows]

    @staticmethod
    def track_out(r, liked: set[str]) -> dict:
        t = track_dict(r)
        return {
            "id": t["id"], "title": t["title"], "artists": t["artists"], "album": t["album"], "album_id": t["album_id"],
            "album_artists": t["album_artists"], "release_date": t["release_date"], "track_number": t["track_number"],
            "disc_number": t["disc_number"], "duration_ms": t["duration_ms"], "explicit": bool(t["explicit"]),
            "cover_url": t["cover_url"], "thumb_url": t["thumb_url"], "isrc": t["isrc"],
            "status": t["status"], "playable": t["status"] == "downloaded",
            "format": Path(t["file_path"]).suffix.lstrip(".") if t["file_path"] else None,
            "bitrate": t["bitrate"], "size": t["file_size"], "liked": t["id"] in liked,
        }

    def library_index(self) -> tuple[dict, dict]:
        """(song key -> track row, isrc -> track row) for matching outside results to the library."""
        with self._index_lock:
            if self._index and time.time() - self._index[0] < 60:
                return self._index[1], self._index[2]
            by_key, by_isrc = {}, {}
            for r in self.db.q("SELECT id, title, artists, isrc, status FROM tracks"):
                by_key.setdefault(discover.song_key(json.loads(r["artists"]), r["title"]), r)
                if r["isrc"]:
                    by_isrc.setdefault(r["isrc"], r)
            self._index = (time.time(), by_key, by_isrc)
            return by_key, by_isrc

    def annotate(self, items: list[dict]) -> list[dict]:
        by_key, by_isrc = self.library_index()
        for it in items:
            r = by_isrc.get(it.get("isrc") or "") or by_key.get(discover.song_key(it["artists"], it["title"]))
            it["in_library"] = r["id"] if r else None
            it["status"] = r["status"] if r else None
        return items

    # -- endpoints ----------------------------------------------------------------

    def me(self, user: dict, q: dict, body: dict) -> dict:
        return {"user": {"id": user["id"], "name": user["name"], "is_admin": bool(user["is_admin"])},
                "spotify_accounts": [], "server": self._server()}  # (always empty: apps up to 1.8 expect it)

    def _server(self) -> dict:
        b = self.svc.branding.info()  # the name and icon to show (Settings → Name and icon)
        return {"name": b["name"], "version": __version__, "icon": b["icon"], "background": b["background"],
                "see_through": b["see_through"],
                # every server's address, active first: the apps switch when one goes away (cluster.py)
                "servers": self.svc.cluster.servers() if self.svc.cluster else []}

    def home(self, user: dict, q: dict, body: dict) -> dict:
        uid = user["id"]
        recent = [r[0] for r in self.db.q("""SELECT track_id FROM plays WHERE user_id = ? GROUP BY track_id
                                             ORDER BY MAX(played_at) DESC LIMIT 20""", (uid,))]
        top = [r[0] for r in self.db.q("""SELECT track_id FROM plays WHERE user_id = ? AND played_at > ?
                                          GROUP BY track_id ORDER BY COUNT(*) DESC, MAX(played_at) DESC LIMIT 20""",
                                       (uid, time.time() - 30 * 86400))]
        lib_sql, lib_params = self.library_sources_sql(uid)
        added = [r[0] for r in self.db.q(f"""SELECT id FROM tracks WHERE status = 'downloaded' AND id IN ({lib_sql})
                                            ORDER BY updated DESC LIMIT 20""", lib_params)]
        requested = [r[0] for r in self.db.q("""SELECT r.track_id FROM requests r JOIN tracks t ON t.id = r.track_id
                                                WHERE r.user_id = ? ORDER BY r.created DESC LIMIT 20""", (uid,))]
        sections = [
            ("recently_played", "Recently played", recent),
            ("top", "Your top songs this month", top),
            ("recently_added", "Recently added", added),
            ("requested", "Your requests", requested),
        ]
        recs = self.svc.recommender.get(uid)
        for_you = None
        if recs:
            following = set(self.svc.podcasts.followed_ids(uid))
            for_you = {"songs": self.songs_out(recs["songs"], uid), "artists": self.artists_out(recs["artists"]),
                       "top_artists": self.artists_out(recs["top_artists"]),
                       "podcasts": [self.podcast_out(p, following) | {"reason": p.get("reason")}
                                    for p in recs["podcasts"] if p["id"] not in following],
                       "computed": recs["computed"]}
        return {"sections": [{"id": sid, "title": title, "tracks": self.tracks_by_ids(ids, uid)}
                             for sid, title, ids in sections if ids],
                "playlists": self.library(user, q, body)["playlists"][:12],
                "liked_count": len(self.liked_ids(uid)),
                "for_you": for_you,
                "podcasts": self.podcast_shelves(uid, 12),
                "releases": self._on_server(self.svc.releases.recent(uid, 20)),
                "unread_notifications": self.svc.releases.unread(uid)}

    def songs_out(self, items: list[dict], uid: int) -> list[dict]:
        """Recommended songs, each with `track` when the server has it (playable once downloaded)."""
        items = [dict(x) for x in items]
        self.annotate([x for x in items if x["source"] != "library"])
        ids = [x["id"] if x["source"] == "library" else x["in_library"] for x in items
               if x["source"] == "library" or x.get("in_library")]
        tracks = {t["id"]: t for t in self.tracks_by_ids(ids, uid)}
        out = []
        for x in items:
            if x["source"] == "library":
                t = tracks.get(x["id"])
                if t is None:
                    continue
                x |= {k: t[k] for k in ("album", "duration_ms", "explicit", "cover_url", "thumb_url", "status")}
                x["in_library"] = t["id"]
            x["track"] = tracks.get(x.get("in_library") or "")
            out.append(x)
        return out

    def artists_out(self, artists: list[dict]) -> list[dict]:
        """Adds how many of each artist's songs are on the server."""
        counts: Counter = Counter()
        for (arts,) in self.db.q("SELECT artists FROM tracks WHERE status = 'downloaded'"):
            for a in json.loads(arts):
                counts[norm(a)] += 1
        return [a | {"songs_on_server": counts[norm(a["name"])]} for a in artists]

    def search(self, user: dict, q: dict, body: dict) -> dict:
        text = (q.get("q") or "").strip()
        if not text:
            return {"tracks": [], "albums": [], "artists": []}
        like = f"%{text}%"
        rows = self.db.q("""SELECT * FROM tracks WHERE status != 'ignored' AND (title LIKE ? OR artists LIKE ? OR album LIKE ?)
                            ORDER BY (status = 'downloaded') DESC, (title LIKE ?) DESC, added_at DESC LIMIT ?""",
                         (like, like, like, f"{text}%", _int(q.get("limit"), 40, 1, 200)))
        liked = self.liked_ids(user["id"])
        tracks = [self.track_out(r, liked) for r in rows]
        albums: dict[str, dict] = {}
        artists: dict[str, dict] = {}
        for t in tracks:
            if t["album_id"] and text.lower() in (t["album"] or "").lower():
                a = albums.setdefault(t["album_id"], {"id": t["album_id"], "name": t["album"], "artists": t["album_artists"],
                                                      "cover_url": t["cover_url"], "year": (t["release_date"] or "")[:4], "count": 0})
                a["count"] += 1
            for name in t["artists"]:
                if text.lower() in name.lower():
                    ar = artists.setdefault(name.lower(), {"name": name, "cover_url": t["cover_url"], "count": 0})
                    ar["count"] += 1
        return {"tracks": tracks, "albums": list(albums.values()), "artists": list(artists.values()),
                "top_artist": self._top_artist(text, artists)}

    @staticmethod
    def _top_artist(text: str, artists: dict[str, dict]) -> dict | None:
        """The artist the search is for, if it names one: shown first, with a bio, even with no songs here."""
        want = norm(text)
        if len(want) < 2:
            return None
        top = next(({"name": a["name"], "songs_on_server": a["count"]} for a in artists.values() if norm(a["name"]) == want), None)
        try:
            found = discover.find_artist(text)
        except (OSError, RuntimeError, ValueError):
            found = None
        if found and norm(found["name"]) == want:
            top = (top or {"name": found["name"], "songs_on_server": 0}) | {
                "image_url": found["image_url"], "thumb_url": found["thumb_url"], "fans": found["fans"]}
        return top

    def lyrics(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        """{synced: [{t: ms, text}] or null, plain, instrumental, source} for a song."""
        row = self.db.one("SELECT id, title, artists, album, duration_ms FROM tracks WHERE id = ?", (tid,))
        if row is None:
            raise ApiError(404, "No such song.")
        found = self.svc.lyrics.get(dict(row))
        if found is None:
            raise ApiError(404, "No lyrics found for this song.")
        return found

    def artist_about(self, user: dict, q: dict, body: dict) -> dict:
        """A short bio, quick facts and history from Wikipedia (null when there's no article)."""
        name = (q.get("name") or "").strip()
        if not name:
            raise ApiError(400, "name is required")
        about, pending = self.svc.artist_info.about(name[:200])
        return {"about": about, "pending": pending}  # pending: ask again in a few seconds

    def track(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        out = self.tracks_by_ids([tid], user["id"])
        if not out:
            raise ApiError(404, "No such song.")
        return out[0]

    # -- the wrong version downloaded: another one instead -------------------------------------------

    def versions(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        """The YouTube uploads found for a song (best match first), the one it has, and how long it
        should be. With ?search=1, YouTube is searched again first."""
        row = self.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))
        if row is None:
            raise ApiError(404, "No such song.")
        if q.get("search") == "1":
            self._search_again(row)
        current = row["youtube_id"] if row["status"] == "downloaded" or row["pinned"] else None
        versions = [{"youtube_id": r["youtube_id"], "title": r["title"], "channel": r["channel"], "duration_s": r["duration"],
                     "score": r["score"], "official": (r["channel"] or "").endswith(" - Topic") or r["source"] in ("ytm", "isrc"),
                     "current": r["youtube_id"] == current, "url": f"https://youtu.be/{r['youtube_id']}"}
                    for r in self.db.q("SELECT * FROM candidates WHERE track_id = ? ORDER BY score DESC LIMIT 15", (tid,))]
        return {"track": self.tracks_by_ids([tid], user["id"])[0], "current": current,
                "current_url": f"https://youtu.be/{current}" if current else None,
                "expected_s": (row["duration_ms"] or 0) / 1000 or None, "versions": versions}

    def _search_again(self, row) -> None:
        if self.svc.cooldown_until > time.time():
            raise ApiError(503, "YouTube asked the server to slow down. Try searching again in a while.")
        t = track_dict(row)
        yt = self.svc.youtube_factory(self.db.settings())
        yt.pace = lambda kind: self.svc.pacer.wait(kind)
        info = TrackInfo(t["title"], t["artists"], t.get("album"), (t.get("duration_ms") or 0) / 1000 or None, t.get("isrc"))
        try:
            _, ranked = yt.find(info, float(self.db.setting("match_threshold") or 0.7))
        except Exception as e:  # noqa: BLE001 (YouTube errors come in many shapes)
            log.info("searching YouTube again for %s failed: %s", t["id"], e)
            raise ApiError(502, "YouTube search isn't working right now. Try again in a minute.") from None
        if ranked:
            self.db.save_candidates(t["id"], ranked)

    def replace(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        """Download another version of this song (one from versions, or any YouTube link), for everyone."""
        vid = youtube_id(str(body.get("youtube") or ""))
        if not vid:
            raise ApiError(400, "That doesn't look like a YouTube link.")
        try:
            self.svc.replace(tid, vid, user["id"])
        except ValueError:
            raise ApiError(404, "No such song.") from None
        except RuntimeError as e:
            raise ApiError(409, str(e)) from None
        return self.tracks_by_ids([tid], user["id"])[0]

    def album(self, user: dict, q: dict, body: dict, album_id: str) -> dict:
        rows = self.db.q("SELECT * FROM tracks WHERE album_id = ? ORDER BY disc_number, track_number", (album_id,))
        if not rows:
            raise ApiError(404, "No such album.")
        liked = self.liked_ids(user["id"])
        tracks = [self.track_out(r, liked) for r in rows]
        first = tracks[0]
        return {"id": album_id, "name": first["album"], "artists": first["album_artists"], "cover_url": first["cover_url"],
                "release_date": first["release_date"], "tracks": tracks}

    def artist(self, user: dict, q: dict, body: dict) -> dict:
        name = (q.get("name") or "").strip()
        if not name:
            raise ApiError(400, "name is required")
        rows = self.db.q("""SELECT t.*, (SELECT COUNT(*) FROM plays p WHERE p.track_id = t.id) AS n FROM tracks t
                            WHERE t.status != 'ignored' AND EXISTS(SELECT 1 FROM json_each(t.artists) WHERE value = ? COLLATE NOCASE)
                            ORDER BY n DESC, t.release_date DESC""", (name,))
        liked = self.liked_ids(user["id"])
        tracks = [self.track_out(r, liked) for r in rows]
        albums: dict[str, dict] = {}
        for t in tracks:
            if t["album_id"]:
                albums.setdefault(t["album_id"], {"id": t["album_id"], "name": t["album"], "cover_url": t["cover_url"],
                                                  "year": (t["release_date"] or "")[:4]})
        about, popular, related = None, [], []
        try:
            about = discover.find_artist(name)
            if about:
                popular = self.annotate([{"source": "deezer", "id": t["deezer_id"], "isrc": None} | t
                                         for t in discover.artist_top(about["deezer_id"], 10)])
                related = discover.related_artists(about["deezer_id"], 12)
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            log.info("artist details for %r unavailable: %s", name, e)
        deezer_id = about["deezer_id"] if about else None
        following = self.svc.releases.follows(user["id"], name, deezer_id) is not None
        return {"name": name, "tracks": tracks, "albums": sorted(albums.values(), key=lambda a: a["year"], reverse=True),
                "about": about, "popular": popular, "related": related, "deezer_id": deezer_id, "following": following}

    # -- following artists ---------------------------------------------------------------------

    def following_artists(self, user: dict, q: dict, body: dict) -> dict:
        return {"artists": self.svc.releases.following(user["id"])}

    def follow_artist(self, user: dict, q: dict, body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "name is required")
        deezer_id = body.get("deezer_id")
        try:
            return self.svc.releases.follow(user["id"], name, int(deezer_id) if deezer_id else None, body.get("image_url"))
        except FollowError as e:
            raise ApiError(404, str(e)) from None
        except (OSError, RuntimeError, ValueError) as e:
            log.info("follow %r failed: %s", name, e)
            raise ApiError(502, "The music catalogue isn't reachable right now; try again soon.") from None

    def unfollow_artist(self, user: dict, q: dict, body: dict) -> dict:
        try:
            deezer_id = int(body.get("deezer_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "deezer_id is required") from None
        self.svc.releases.unfollow(user["id"], deezer_id)
        return {"ok": True}

    def new_releases(self, user: dict, q: dict, body: dict) -> dict:
        return {"releases": self._on_server(self.svc.releases.recent(user["id"], _int(q.get("limit"), 30, 1, 100)))}

    def notifications(self, user: dict, q: dict, body: dict) -> dict:
        return self.svc.releases.notifications(user["id"], _int(q.get("limit"), 50, 1, 200))

    def read_notifications(self, user: dict, q: dict, body: dict) -> dict:
        ids = [int(i) for i in body.get("ids") or [] if str(i).isdigit()]
        self.svc.releases.mark_read(user["id"], ids or None)
        return {"unread": self.svc.releases.unread(user["id"])}

    def library(self, user: dict, q: dict, body: dict) -> dict:
        uid = user["id"]
        out = []
        for r in self.db.q("""SELECT p.*, COUNT(t.track_id) AS n FROM user_playlists p
                              LEFT JOIN user_playlist_tracks t ON t.playlist_id = p.id
                              WHERE p.user_id = ? GROUP BY p.id ORDER BY p.updated DESC, p.name COLLATE NOCASE""", (uid,)):
            out.append({"id": r["id"], "name": r["name"], "kind": "app", "owner": user["name"], "description": r["description"],
                        "image_url": r["image_url"], "count": r["n"], "editable": True, "from_spotify": bool(r["source_id"])})
        # the person's own arrangement; playlists they haven't placed yet (new ones) go first
        order = {r[0]: r[1] for r in self.db.q("SELECT playlist_id, position FROM playlist_order WHERE user_id = ?", (uid,))}
        if order:
            out = [p for p in out if p["id"] not in order] + sorted((p for p in out if p["id"] in order), key=lambda p: order[p["id"]])
        return {"liked": {"count": len(self.liked_ids(uid))}, "playlists": out}

    def set_playlist_order(self, user: dict, q: dict, body: dict) -> dict:
        """Arrange the playlists in your library: `playlist_ids` in the order you want them."""
        ids = body.get("playlist_ids")
        if not isinstance(ids, list):
            raise ApiError(400, "playlist_ids must be a list.")
        mine = {p["id"] for p in self.library(user, q, body)["playlists"]}
        ids = [i for i in dict.fromkeys(str(i) for i in ids[:5000]) if i in mine]
        with self.db.tx() as c:
            c.execute("DELETE FROM playlist_order WHERE user_id = ?", (user["id"],))
            c.executemany("INSERT INTO playlist_order(user_id, playlist_id, position) VALUES (?, ?, ?)",
                          [(user["id"], pid, n) for n, pid in enumerate(ids)])
        return self.library(user, q, body)

    def liked(self, user: dict, q: dict, body: dict) -> dict:
        offset, limit = _int(q.get("offset"), 0, 0, 10**6), _int(q.get("limit"), 100, 1, 500)
        ids = self.db.liked_list(user["id"])
        return {"total": len(ids), "offset": offset, "tracks": self.tracks_by_ids(ids[offset: offset + limit], user["id"])}

    def move_in_liked(self, user: dict, q: dict, body: dict) -> dict:
        """Move the song at position `from` of your Liked Songs to `to`. `track_id` is the song you
        mean: if the list changed meanwhile, nothing moves (409)."""
        uid = user["id"]
        ids = self.db.liked_list(uid)
        src, dst = _int(body.get("from"), -1, -1, 10**6), _int(body.get("to"), -1, -1, 10**6)
        if not (0 <= src < len(ids) and 0 <= dst < len(ids)):
            raise ApiError(400, "There's no song at that position.")
        if body.get("track_id") is not None and ids[src] != str(body["track_id"]):
            raise ApiError(409, "Your Liked Songs changed somewhere else. They've been reloaded: try again.")
        ids.insert(dst, ids.pop(src))
        with self.db.tx() as c:
            c.execute("DELETE FROM liked_order WHERE user_id = ?", (uid,))
            c.executemany("INSERT INTO liked_order(user_id, track_id, position) VALUES (?, ?, ?)",
                          [(uid, tid, n) for n, tid in enumerate(ids)])
        return {"ok": True}

    def _playlist_access(self, uid: int, pid: str) -> str:
        if self.db.one("SELECT 1 FROM user_playlists WHERE id = ? AND user_id = ?", (pid, uid)):
            return "app"
        raise ApiError(404, "No such playlist.")

    def playlist(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        if not pid.startswith("up"):  # an older app remembering a Spotify playlist: show their own copy of it
            copy = self.db.one("SELECT id FROM user_playlists WHERE user_id = ? AND source_id = ?", (user["id"], pid))
            pid = copy[0] if copy else pid
        self._playlist_access(user["id"], pid)
        offset, limit = _int(q.get("offset"), 0, 0, 10**6), _int(q.get("limit"), 100, 1, 500)
        meta = self.db.one("SELECT * FROM user_playlists WHERE id = ?", (pid,))
        ids = [r[0] for r in self.db.q("SELECT track_id FROM user_playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]
        info = {"id": pid, "name": meta["name"], "description": meta["description"], "kind": "app", "editable": True,
                "owner": user["name"], "image_url": meta["image_url"], "from_spotify": bool(meta["source_id"])}
        return info | {"total": len(ids), "offset": offset, "tracks": self.tracks_by_ids(ids[offset: offset + limit], user["id"])}

    def create_playlist(self, user: dict, q: dict, body: dict) -> dict:
        name = str(body.get("name") or "").strip()[:100]
        if not name:
            raise ApiError(400, "Give the playlist a name.")
        pid = "up" + secrets.token_hex(8)
        now = time.time()
        self.db.run("INSERT INTO user_playlists(id, user_id, name, description, created, updated) VALUES (?,?,?,?,?,?)",
                    (pid, user["id"], name, str(body.get("description") or "")[:500] or None, now, now))
        return self.playlist(user, {}, {}, pid)

    def _own_playlist(self, user: dict, pid: str) -> None:
        if self._playlist_access(user["id"], pid) != "app":
            raise ApiError(403, "That playlist can't be changed.")

    def update_playlist(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        self._own_playlist(user, pid)
        if "name" in body:
            name = str(body["name"]).strip()[:100]
            if not name:
                raise ApiError(400, "Give the playlist a name.")
            self.db.run("UPDATE user_playlists SET name = ? WHERE id = ?", (name, pid))
        if "description" in body:
            self.db.run("UPDATE user_playlists SET description = ? WHERE id = ?", (str(body["description"] or "")[:500] or None, pid))
        self.db.run("UPDATE user_playlists SET updated = ? WHERE id = ?", (time.time(), pid))
        return self.playlist(user, {}, {}, pid)

    def delete_playlist(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        self._own_playlist(user, pid)
        with self.db.tx() as c:
            c.execute("DELETE FROM user_playlist_tracks WHERE playlist_id = ?", (pid,))
            c.execute("DELETE FROM user_playlists WHERE id = ?", (pid,))
            c.execute("DELETE FROM track_sources WHERE source = ?", (f"up:{pid}",))
        self.db.recompute_monitored()
        return {"ok": True}

    def add_to_playlist(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        self._own_playlist(user, pid)
        ids = [str(i) for i in body.get("track_ids") or []][:500]
        known = {r[0] for r in self.db.q(f"SELECT id FROM tracks WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        ids = [i for i in ids if i in known]
        with self.db.tx() as c:
            start = c.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM user_playlist_tracks WHERE playlist_id = ?", (pid,)).fetchone()[0]
            c.executemany("INSERT INTO user_playlist_tracks(playlist_id, position, track_id, added_at) VALUES (?,?,?,?)",
                          [(pid, start + i, tid, time.time()) for i, tid in enumerate(ids)])
            c.executemany("INSERT OR IGNORE INTO track_sources(track_id, source) VALUES (?, ?)", [(tid, f"up:{pid}") for tid in ids])
            c.execute("UPDATE user_playlists SET updated = ? WHERE id = ?", (time.time(), pid))
        self.db.recompute_monitored()
        return {"added": len(ids)}

    def remove_from_playlist(self, user: dict, q: dict, body: dict, pid: str, position: str) -> dict:
        self._own_playlist(user, pid)
        pos = _int(position, -1, -1, 10**6)
        with self.db.tx() as c:
            row = c.execute("SELECT track_id FROM user_playlist_tracks WHERE playlist_id = ? AND position = ?", (pid, pos)).fetchone()
            if row is None:
                raise ApiError(404, "Nothing at that position.")
            c.execute("DELETE FROM user_playlist_tracks WHERE playlist_id = ? AND position = ?", (pid, pos))
            c.execute("UPDATE user_playlist_tracks SET position = position - 1 WHERE playlist_id = ? AND position > ?", (pid, pos))
            if not c.execute("SELECT 1 FROM user_playlist_tracks WHERE playlist_id = ? AND track_id = ?", (pid, row[0])).fetchone():
                c.execute("DELETE FROM track_sources WHERE track_id = ? AND source = ?", (row[0], f"up:{pid}"))
            c.execute("UPDATE user_playlists SET updated = ? WHERE id = ?", (time.time(), pid))
        self.db.recompute_monitored()
        return {"ok": True}

    def move_in_playlist(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        """Move the song at position `from` to position `to` (the songs between shift along).
        `track_id` is the song you mean to move: if the playlist changed meanwhile (on another
        device), nothing moves and you get 409."""
        self._own_playlist(user, pid)
        src, dst = _int(body.get("from"), -1, -1, 10**6), _int(body.get("to"), -1, -1, 10**6)
        with self.db.tx() as c:
            rows = [tuple(r) for r in c.execute(
                "SELECT track_id, added_at FROM user_playlist_tracks WHERE playlist_id = ? ORDER BY position", (pid,))]
            if not (0 <= src < len(rows) and 0 <= dst < len(rows)):
                raise ApiError(400, "There's no song at that position.")
            if body.get("track_id") is not None and rows[src][0] != str(body["track_id"]):
                raise ApiError(409, "This playlist changed somewhere else. It's been reloaded: try again.")
            rows.insert(dst, rows.pop(src))
            c.execute("DELETE FROM user_playlist_tracks WHERE playlist_id = ?", (pid,))
            c.executemany("INSERT INTO user_playlist_tracks(playlist_id, position, track_id, added_at) VALUES (?,?,?,?)",
                          [(pid, n, tid, added) for n, (tid, added) in enumerate(rows)])
            c.execute("UPDATE user_playlists SET updated = ? WHERE id = ?", (time.time(), pid))
        return {"ok": True}

    def like(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        if not self.db.one("SELECT 1 FROM tracks WHERE id = ?", (tid,)):
            raise ApiError(404, "No such song.")
        self.db.run("INSERT OR IGNORE INTO likes(user_id, track_id, created) VALUES (?, ?, ?)", (user["id"], tid, time.time()))
        self.db.run("DELETE FROM liked_order WHERE user_id = ? AND track_id = ?", (user["id"], tid))  # a new like goes first
        self.db.add_source(tid, f"like:{user['id']}")
        self.db.recompute_monitored()
        return {"liked": True}

    def unlike(self, user: dict, q: dict, body: dict, tid: str) -> dict:
        self.db.run("DELETE FROM likes WHERE user_id = ? AND track_id = ?", (user["id"], tid))
        self.db.remove_source(tid, f"like:{user['id']}")
        self.db.recompute_monitored()
        return {"liked": False, "on_spotify": False}  # (on_spotify: apps up to 1.8 expect it)

    def play(self, user: dict, q: dict, body: dict) -> dict:
        tid = str(body.get("track_id") or "")
        if not self.db.one("SELECT 1 FROM tracks WHERE id = ?", (tid,)):
            raise ApiError(404, "No such song.")
        self.db.run("INSERT INTO plays(user_id, track_id, played_at, ms_played, completed) VALUES (?,?,?,?,?)",
                    (user["id"], tid, time.time(), _int(body.get("ms_played"), 0, 0, 10**8), int(bool(body.get("completed")))))
        return {"ok": True}

    # -- podcasts --------------------------------------------------------------------

    @staticmethod
    def podcast_out(p, following: set[str]) -> dict:
        p = dict(p)
        return {"id": p["id"], "title": p["title"], "author": p.get("author"), "artwork_url": p.get("artwork_url"),
                "genre": p.get("genre"), "following": p["id"] in following}

    @staticmethod
    def episode_out(r, progress: dict | None = None, notes: bool = False, kept: dict | None = None) -> dict:
        r = dict(r)
        pos, done = (progress or {}).get(r["id"], (0, False))
        k = (kept or {}).get(r["id"], {})
        out = {"id": r["id"], "podcast_id": r["podcast_id"], "podcast_title": r.get("podcast_title"), "title": r["title"],
               "published": r["published"], "duration_ms": r["duration_ms"],
               "image_url": r.get("image_url") or r.get("artwork_url"), "progress_ms": pos, "completed": bool(done),
               "keep": k.get("keep"), "expires": k.get("expires"), "on_server": bool(k.get("on_server")), "size": k.get("size")}
        if notes:
            out["description"] = r.get("description")
        return out

    def _progress(self, uid: int, episode_ids: list[str]) -> dict[str, tuple[int, bool]]:
        if not episode_ids:
            return {}
        return {r[0]: (r[1], bool(r[2])) for r in self.db.q(
            f"""SELECT episode_id, position_ms, completed FROM episode_progress WHERE user_id = ?
                AND episode_id IN ({','.join('?' * len(episode_ids))})""", [uid, *episode_ids])}

    def podcast_shelves(self, uid: int, limit: int = 50) -> dict:
        """Followed shows, episodes in progress, and new episodes of followed shows."""
        store = self.svc.podcasts
        ids = store.followed_ids(uid)
        following = set(ids)
        rows = {r["id"]: r for r in self.db.q(f"SELECT * FROM podcasts WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        shows = [self.podcast_out(rows[i], following) for i in ids if i in rows][:limit]
        cont = self.db.q("""SELECT e.*, p.title AS podcast_title, p.artwork_url, ep.position_ms, ep.completed
                            FROM episode_progress ep JOIN podcast_episodes e ON e.id = ep.episode_id
                            JOIN podcasts p ON p.id = e.podcast_id
                            WHERE ep.user_id = ? AND ep.completed = 0 AND ep.position_ms > 15000
                            ORDER BY ep.updated DESC LIMIT 10""", (uid,))
        new = self.db.q(f"""SELECT e.*, p.title AS podcast_title, p.artwork_url FROM podcast_episodes e
                            JOIN podcasts p ON p.id = e.podcast_id
                            WHERE e.podcast_id IN ({','.join('?' * len(ids)) or 'NULL'}) AND e.published > ?
                            AND e.id NOT IN (SELECT episode_id FROM episode_progress WHERE user_id = ?)
                            ORDER BY e.published DESC LIMIT 15""", [*ids, time.time() - 21 * 86400, uid])
        kept = self.svc.podcast_downloads.info(uid, [r["id"] for r in [*cont, *new]])
        return {"following": shows,
                "continue": [self.episode_out(r, {r["id"]: (r["position_ms"], r["completed"])}, kept=kept) for r in cont],
                "new_episodes": [self.episode_out(r, kept=kept) for r in new]}

    def podcasts_home(self, user: dict, q: dict, body: dict) -> dict:
        uid = user["id"]
        shelves = self.podcast_shelves(uid)
        recs = self.svc.recommender.get(uid) or {}
        following = {p["id"] for p in shelves["following"]}
        shelves["recommended"] = [self.podcast_out(p, following) | {"reason": p.get("reason")}
                                  for p in recs.get("podcasts", []) if p["id"] not in following]
        return shelves

    def podcast_search(self, user: dict, q: dict, body: dict) -> dict:
        text = (q.get("q") or "").strip()
        if not text:
            return {"results": []}
        try:
            found = podcasts.search(text, _int(q.get("limit"), 20, 1, 50))
        except (podcasts.PodcastError, OSError, ValueError) as e:
            raise ApiError(502, f"Couldn't search podcasts right now ({e}).") from None
        self.svc.podcasts.upsert(found)
        following = set(self.svc.podcasts.followed_ids(user["id"]))
        return {"results": [self.podcast_out(p, following) for p in found]}

    def podcast(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        store = self.svc.podcasts
        stale = False
        try:
            show = store.refresh(pid)
        except (podcasts.PodcastError, OSError, ValueError) as e:
            log.info("podcast %s: feed unavailable: %s", pid, e)
            try:
                show = store.get(pid)
            except (podcasts.PodcastError, OSError, ValueError):
                show = None
            if show is None:
                raise ApiError(502 if isinstance(e, OSError) else 404, str(e) or "Couldn't load this podcast.") from None
            stale = True
        uid = user["id"]
        rows = self.db.q("SELECT * FROM podcast_episodes WHERE podcast_id = ? ORDER BY published DESC LIMIT ?",
                         (pid, _int(q.get("limit"), 100, 1, 300)))
        progress = self._progress(uid, [r["id"] for r in rows])
        kept = self.svc.podcast_downloads.info(uid, [r["id"] for r in rows])
        following = set(store.followed_ids(uid))
        return self.podcast_out(show, following) | {
            "description": show.get("description"), "website": show.get("website"), "on_spotify": False,
            "stale": stale, "downloads": self.svc.podcast_downloads.keep_for(uid, pid),
            "episodes": [self.episode_out({**dict(r), "podcast_title": show["title"], "artwork_url": show.get("artwork_url")},
                                          progress, notes=True, kept=kept) for r in rows]}

    def set_podcast_downloads(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        """Download this show's new episodes: 'off', until 'played', or for a number of days ('3', '7', '14', '30')."""
        if self.svc.podcasts.get(pid) is None:
            raise ApiError(404, "No such podcast.")
        try:
            self.svc.podcast_downloads.set_keep(user["id"], pid, body.get("keep"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return {"downloads": self.svc.podcast_downloads.keep_for(user["id"], pid)}

    def keep_episode(self, user: dict, q: dict, body: dict, eid: str) -> dict:
        if not self.db.one("SELECT 1 FROM podcast_episodes WHERE id = ?", (eid,)):
            raise ApiError(404, "No such episode.")
        try:
            self.svc.podcast_downloads.hold(user["id"], eid, body.get("keep") or "played")
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return self.svc.podcast_downloads.info(user["id"], [eid]).get(eid, {})

    def drop_episode(self, user: dict, q: dict, body: dict, eid: str) -> dict:
        self.svc.podcast_downloads.unhold(user["id"], eid)
        return {"ok": True}

    def podcast_downloads(self, user: dict, q: dict, body: dict) -> dict:
        """Episodes this profile keeps downloaded; the apps mirror the ones on the server for offline use."""
        uid = user["id"]
        rows = self.db.q("""SELECT e.*, p.title AS podcast_title, p.artwork_url FROM episode_holds h
                            JOIN podcast_episodes e ON e.id = h.episode_id JOIN podcasts p ON p.id = e.podcast_id
                            WHERE h.user_id = ? AND h.released IS NULL ORDER BY e.published DESC""", (uid,))
        ids = [r["id"] for r in rows]
        return {"episodes": [self.episode_out(r, self._progress(uid, ids), kept=self.svc.podcast_downloads.info(uid, ids)) for r in rows],
                "downloading": self.svc.podcast_downloads.active}

    def follow_podcast(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        try:
            self.svc.podcasts.follow(user["id"], pid)
        except podcasts.PodcastError as e:
            raise ApiError(404, str(e)) from None
        except OSError:
            raise ApiError(502, "Couldn't look the podcast up right now.") from None
        self.svc.recommender.schedule(user["id"])
        return {"following": True}

    def unfollow_podcast(self, user: dict, q: dict, body: dict, pid: str) -> dict:
        self.svc.podcasts.unfollow(user["id"], pid)
        self.svc.recommender.schedule(user["id"])
        return {"following": False, "on_spotify": False}  # (on_spotify: apps up to 1.8 expect it)

    def episode_progress(self, user: dict, q: dict, body: dict, eid: str) -> dict:
        if not self.db.one("SELECT 1 FROM podcast_episodes WHERE id = ?", (eid,)):
            raise ApiError(404, "No such episode.")
        duration = body.get("duration_ms")
        self.svc.podcasts.set_progress(user["id"], eid, _int(body.get("position_ms"), 0, 0, 10**9),
                                       _int(duration, 0, 0, 10**9) if duration is not None else None, bool(body.get("completed")))
        if body.get("completed"):
            self.svc.podcast_downloads.wake.set()  # "until played" downloads go now
        return {"ok": True}

    # -- Jams: listening together -----------------------------------------------------------

    def people(self, user: dict, q: dict, body: dict) -> dict:
        """The other family profiles, to invite to a Jam."""
        return {"people": [{"id": r["id"], "name": r["name"]} for r in
                           self.db.q("SELECT id, name FROM users WHERE id != ? ORDER BY name COLLATE NOCASE", (user["id"],))]}

    def _names(self) -> dict[int, str]:
        return {r[0]: r[1] for r in self.db.q("SELECT id, name FROM users")}

    def _jam_items(self, uid: int, items: list) -> list[dict]:
        """[{track_id} | {episode_id}] -> playable queue entries (songs as track objects, episodes as their stand-ins)."""
        items = [i for i in items if isinstance(i, dict)][:500]
        tracks = {t["id"]: t for t in self.tracks_by_ids([str(i["track_id"]) for i in items if i.get("track_id")], uid)}
        ep_ids = [str(i["episode_id"]) for i in items if i.get("episode_id")]
        eps = {r["id"]: r for r in self.db.q(
            f"""SELECT e.*, p.title AS podcast_title, p.artwork_url FROM podcast_episodes e JOIN podcasts p ON p.id = e.podcast_id
                WHERE e.id IN ({','.join('?' * len(ep_ids)) or 'NULL'})""", ep_ids)}
        out = []
        for i in items:
            if i.get("track_id"):
                t = tracks.get(str(i["track_id"]))
                if t and t["playable"]:
                    out.append(t)
            elif i.get("episode_id") and (e := eps.get(str(i["episode_id"]))):
                art = e["image_url"] or e["artwork_url"]
                out.append({"id": e["id"], "kind": "episode", "podcast_id": e["podcast_id"], "title": e["title"],
                            "artists": [e["podcast_title"]], "album": e["podcast_title"], "duration_ms": e["duration_ms"],
                            "cover_url": art, "thumb_url": art, "status": "downloaded", "playable": True})
        return out

    def jam_more(self, jam: Jam) -> list[dict]:
        """More songs when a Jam's queue runs out: songs everyone in it likes first, then anyone's
        likes, leaning toward the artists just played, never repeating what the Jam already had.
        Only songs on the server (everyone streams them). A Jam of podcasts just ends."""
        last = jam.queue[jam.index] if 0 <= jam.index < len(jam.queue) else None
        if last is None or last.get("kind") == "episode":
            return []
        had = {t["id"] for t in jam.queue}
        likers: Counter[str] = Counter()
        for uid in jam.members:
            likers.update(self.liked_ids(uid))
        recent = {a.lower() for t in jam.queue[max(0, jam.index - 4):jam.index + 1] for a in t.get("artists") or []}
        ids = [i for i in likers if i not in had]
        rows = {r["id"]: r for r in self.db.q(
            f"SELECT * FROM tracks WHERE status = 'downloaded' AND id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        if not rows:  # nobody's likes are downloaded yet: anything on the server
            rows = {r["id"]: r for r in self.db.q(
                "SELECT * FROM tracks WHERE status = 'downloaded' ORDER BY RANDOM() LIMIT 200") if r["id"] not in had}

        def score(tid: str) -> float:
            artists = {a.lower() for a in (track_dict(rows[tid])["artists"] or [])}
            return likers[tid] * 3 + (1 if artists & recent else 0) + random.random() * 0.9  # more likers always wins

        picked = sorted(rows, key=score, reverse=True)[:jams.TOP_UP]
        # each person sees their own likes; the queue holds the plain track
        return [dict(t, jam_added=True) for t in self.tracks_by_ids(picked, 0)]

    def _jam_out(self, jam: Jam, uid: int) -> dict:
        state = jam.state(self._names())
        liked = self.liked_ids(uid)  # likes are personal: show each person their own
        state["queue"] = [dict(t, liked=t["id"] in liked) if t.get("kind") != "episode" else t for t in state["queue"]]
        return state

    def start_jam(self, user: dict, q: dict, body: dict) -> dict:
        items = self._jam_items(user["id"], body.get("items") or [])
        invite = [int(u) for u in body.get("invite") or [] if str(u).isdigit()]
        known = {r[0] for r in self.db.q("SELECT id FROM users")}
        jam = self.svc.jams.start(user["id"], items, _int(body.get("index"), 0, 0, 10**6),
                                  _int(body.get("position_ms"), 0, 0, 10**9), bool(body.get("playing", True)),
                                  [u for u in invite if u in known])
        self._notify_invited(user, jam, list(jam.invited))
        return self._jam_out(jam, user["id"])

    def _notify_invited(self, user: dict, jam: Jam, user_ids: list[int]) -> None:
        if not user_ids:
            return
        item = jam.queue[jam.index] if jam.queue else {}
        what = f"{item.get('title', '')} · {', '.join(item.get('artists') or [])}".strip(" ·")
        self.svc.push.send_async(user_ids, f"{user['name']} started a Jam", f"Listen together: {what}" if what else "Listen together",
                                 {"type": "jam_invite", "jam": jam.id})

    def current_jam(self, user: dict, q: dict, body: dict) -> dict:
        mine, invites = self.svc.jams.current(user["id"])
        names = self._names()
        return {"jam": self._jam_out(mine, user["id"]) if mine else None,
                "invites": [{"id": j.id, "from": names.get(j.host, ""), "members": [names.get(u, "") for u in j.members],
                             "item": j.queue[j.index] if j.queue else None} for j in invites],
                "server_time": int(time.time() * 1000)}

    def jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        """The Jam's state; with ?v=<version> waits (up to 25 s) until it changes."""
        if q.get("v") is None:
            return self._jam_out(self.svc.jams.get(jam_id, user["id"]), user["id"])
        jam = self.svc.jams.wait(jam_id, user["id"], _int(q.get("v"), 0, -1, 10**9), timeout=_int(q.get("wait"), 25, 0, 25))
        return self._jam_out(jam, user["id"])

    def join_jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        return self._jam_out(self.svc.jams.join(jam_id, user["id"]), user["id"])

    def decline_jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        self.svc.jams.decline(jam_id, user["id"])
        return {"ok": True}

    def leave_jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        self.svc.jams.leave(jam_id, user["id"])
        return {"ok": True}

    def invite_to_jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        known = {r[0] for r in self.db.q("SELECT id FROM users")}
        others = [int(u) for u in body.get("invite") or [] if str(u).isdigit() and int(u) in known]
        before = set(self.svc.jams.get(jam_id, user["id"]).invited)
        jam = self.svc.jams.invite(jam_id, user["id"], others)
        self._notify_invited(user, jam, [u for u in jam.invited if u not in before])
        return self._jam_out(jam, user["id"])

    def control_jam(self, user: dict, q: dict, body: dict, jam_id: str) -> dict:
        action = str(body.get("action") or "")
        items = self._jam_items(user["id"], body.get("items") or []) if action in ("add", "replace") else None
        return self._jam_out(self.svc.jams.control(jam_id, user["id"], action, body, items), user["id"])

    # -- this profile's sign-in password -----------------------------------------------

    def account(self, user: dict, q: dict, body: dict) -> dict:
        return self.users.account(user["id"])

    def set_password(self, user: dict, q: dict, body: dict) -> dict:
        """Set or change the password for signing in on other devices without a QR code."""
        try:
            self.users.change_password(user["id"], str(body.get("login") or ""), str(body.get("current") or ""),
                                       str(body.get("password") or ""), f"device:{user['device_id']}")
        except SignInError as e:
            raise ApiError(e.status, str(e)) from None
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return self.users.account(user["id"])

    def push_token(self, user: dict, q: dict, body: dict) -> dict:
        """The phone's Firebase registration token, so Jam invites reach it when the app is closed."""
        token = str(body.get("token") or "").strip()
        if len(token) > 4096:
            raise ApiError(400, "That token is too long.")
        self.db.run("UPDATE devices SET push_token = ? WHERE id = ?", (token or None, user["device_id"]))
        return {"ok": True, "push": self.svc.push.configured}

    # -- app updates -------------------------------------------------------------------

    def app_update(self, user: dict, q: dict, body: dict) -> dict:
        """Is there a newer app for this phone? `build` is the app's build number, `abi` its processor type."""
        abi = q.get("abi") or ""
        if abi not in app_updates.ABIS:
            raise ApiError(400, f"abi must be one of {', '.join(app_updates.ABIS)}")
        latest = self.svc.app_updates.latest(abi)
        have = _int(q.get("build"), 0, 0, 10**9)
        return {"available": bool(latest and latest["build"] > have), "update": latest}

    # -- discovering and requesting songs ------------------------------------------

    def catalog_search(self, user: dict, q: dict, body: dict) -> dict:
        text = (q.get("q") or "").strip()
        if not text:
            return {"source": None, "results": []}
        found = discover.search(text, 20)
        return {"source": "deezer", "results": self.annotate([{"source": "deezer", "id": t["deezer_id"], "isrc": None} | t for t in found])}

    def request(self, user: dict, q: dict, body: dict) -> dict:
        source, ext_id = body.get("source"), str(body.get("id") or "")
        if source == "deezer" and ext_id.isdigit():
            track = discover.full_track(int(ext_id))
        elif source == "spotify" and (row := self.db.one("SELECT * FROM tracks WHERE id = ?", (ext_id,))):
            track = track_dict(row)  # (an older app asking for a song the server already knows)
        elif source == "spotify":
            raise ApiError(400, "Search for the song again: songs come from Deezer's catalogue now.")
        else:
            raise ApiError(400, "Send {source: 'deezer', id: ...} from a search or chart result.")
        track.setdefault("added_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        by_key, by_isrc = self.library_index()
        existing = by_isrc.get(track.get("isrc") or "") or by_key.get(discover.song_key(track["artists"], track["title"]))
        if existing and existing["id"] != track["id"]:
            track = track_dict(self.db.one("SELECT * FROM tracks WHERE id = ?", (existing["id"],)))
        self._index = None
        row = self.svc.request_track(user["id"], track)
        if body.get("like"):  # "Like" on a preview: save it to Liked Songs as well as downloading it
            self.like(user, {}, {}, row["id"])
        return self.track_out(self.db.one("SELECT * FROM tracks WHERE id = ?", (row["id"],)), self.liked_ids(user["id"]))

    # -- whole albums -------------------------------------------------------------------

    def catalog_albums(self, user: dict, q: dict, body: dict) -> dict:
        """Albums anywhere, to add whole: soundtracks, film scores, live albums. Each says how many of
        its songs are already on the server (by album name and artist)."""
        text = (q.get("q") or "").strip()
        if not text:
            return {"albums": []}
        try:
            found = discover.search_albums(text, _int(q.get("limit"), 20, 1, 50))
        except (OSError, RuntimeError, ValueError) as e:
            log.info("album search failed: %s", e)
            raise ApiError(502, "Album search isn't available right now.") from None
        return {"albums": self._on_server(found)}

    def _on_server(self, albums: list[dict]) -> list[dict]:
        """Each album, with how many of its songs are already on the server (by album name and artist)."""
        have: dict[tuple[str, str], int] = {}
        for r in self.db.q("""SELECT album, album_artists, COUNT(*) FROM tracks WHERE album IS NOT NULL AND status = 'downloaded'
                              GROUP BY album, album_artists"""):
            artists = json.loads(r[1] or "[]")
            key = (norm(r[0]), norm(artists[0] if artists else ""))
            have[key] = have.get(key, 0) + r[2]
        for a in albums:
            a["on_server"] = have.get((norm(a["name"]), norm(a["artists"][0] if a["artists"] else "")), 0)
        return albums

    def _album_matches(self, tracks: list[dict]) -> list:
        """The library's copy of each album track, or None. Same recording (ISRC), or same title and
        artist on an album of the same name: film scores reuse titles like "Main Title" across albums,
        so a title and artist alone aren't enough."""
        isrcs = [t["isrc"] for t in tracks if t.get("isrc")]
        titles = list({t["title"] for t in tracks})
        rows = self.db.q(f"""SELECT id, title, artists, album, isrc, status FROM tracks
                             WHERE isrc IN ({','.join('?' * len(isrcs)) or 'NULL'}) OR title IN ({','.join('?' * len(titles)) or 'NULL'})""",
                         [*isrcs, *titles])
        by_isrc = {r["isrc"]: r for r in rows if r["isrc"]}
        by_key: dict[tuple[str, str], object] = {}
        for r in rows:
            by_key.setdefault((discover.song_key(json.loads(r["artists"]), r["title"]), norm(r["album"] or "")), r)
        return [by_isrc.get(t.get("isrc") or "") or by_key.get((discover.song_key(t["artists"], t["title"]), norm(t["album"] or "")))
                for t in tracks]

    def _album_out(self, album: dict, uid: int) -> dict:
        matches = self._album_matches(album["tracks"])
        ids = [m["id"] for m in matches if m is not None]
        rows = {r["id"]: r for r in self.db.q(f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        mine = set(ids) & {r[0] for r in self.db.q("SELECT track_id FROM track_sources WHERE source = ?", (f"req:{uid}",))}
        liked = self.liked_ids(uid)
        tracks = []
        for t, m in zip(album["tracks"], matches):
            row = rows.get(m["id"]) if m is not None else None
            tracks.append({"source": "deezer", "id": t["deezer_id"], "isrc": t["isrc"]}
                          | {k: t[k] for k in ("title", "artists", "album", "duration_ms", "explicit", "cover_url", "thumb_url",
                                                "track_number", "disc_number")}
                          | {"in_library": row["id"] if row else None, "status": row["status"] if row else None,
                             "track": self.track_out(row, liked) if row else None})
        on_server = sum(t["status"] == "downloaded" for t in tracks)
        added = all(t["in_library"] in mine or t["in_library"] in liked for t in tracks)  # all in your library
        return {k: album[k] for k in ("source", "id", "name", "artists", "type", "release_date", "label", "genres", "explicit",
                                      "cover_url", "thumb_url")} | {
            "year": (album.get("release_date") or "")[:4] or None, "count": len(tracks), "on_server": on_server,
            "added": bool(tracks) and added, "tracks": tracks}

    def _load_album(self, source: str, ext_id: str) -> dict:
        if source != "deezer" or not ext_id.isdigit():
            raise ApiError(400, "Send {source: 'deezer', id: ...} from an album search result.")
        try:
            album = discover.album(int(ext_id))
        except RuntimeError:
            raise ApiError(404, "No such album.") from None
        except (OSError, ValueError) as e:
            log.info("album lookup failed: %s", e)
            raise ApiError(502, "Couldn't look that album up right now.") from None
        if not album["tracks"]:
            raise ApiError(404, "That album has no songs available.")
        return album

    def catalog_album(self, user: dict, q: dict, body: dict, source: str, ext_id: str) -> dict:
        """One album with its full track list, and which songs are already on the server."""
        return self._album_out(self._load_album(source, ext_id), user["id"])

    def request_album(self, user: dict, q: dict, body: dict) -> dict:
        """Add a whole album: songs not on the server download next, in album order; songs already
        there join your library."""
        album = self._load_album(str(body.get("source") or ""), str(body.get("id") or ""))
        matches = self._album_matches(album["tracks"])
        ids = []
        for t, m in zip(album["tracks"], matches):
            if m is not None:  # already on the server (maybe from someone's Spotify): keep its details
                t = track_dict(self.db.one("SELECT * FROM tracks WHERE id = ?", (m["id"],)))
            ids.append(self.svc.request_track(user["id"], t, log=False)["id"])
        base = time.time()
        with self.db.tx() as c:  # newest request first, so count down: the album's first song goes first
            c.executemany("UPDATE tracks SET requested_at = ? WHERE id = ?", [(base - n / 1000, tid) for n, tid in enumerate(ids)])
        new = sum(m is None for m in matches)
        self.db.log("requested", f"{user['name']} added the album {', '.join(album['artists'])} - {album['name']} "
                                 f"({len(ids)} songs, {new} new)")
        self._index = None
        self.svc.wake.set()
        return self._album_out(album, user["id"])

    def requests(self, user: dict, q: dict, body: dict) -> dict:
        ids = [r[0] for r in self.db.q("SELECT track_id FROM requests WHERE user_id = ? ORDER BY created DESC LIMIT 200", (user["id"],))]
        return {"requests": self.tracks_by_ids(ids, user["id"])}

    def genres(self, user: dict, q: dict, body: dict) -> dict:
        return {"genres": discover.genres()}

    def genre(self, user: dict, q: dict, body: dict, gid: str) -> dict:
        items = [{"source": "deezer", "id": t["deezer_id"], "isrc": None} | t
                 for t in discover.genre_chart(_int(gid, 0, 0, 10**9), _int(q.get("limit"), 50, 1, 100))]
        return {"genre_id": int(gid), "tracks": self.annotate(items)}


def make_app_server(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    api = AppAPI(svc)
    svc.jams.more = api.jam_more
    routes: list[tuple[str, re.Pattern, Callable, bool]] = []

    def route(method: str, pattern: str, fn: Callable, auth: bool = True) -> None:
        routes.append((method, re.compile(f"^/api/v1{pattern}$"), fn, auth))

    route("GET", "/me", api.me)
    route("GET", "/home", api.home)
    route("GET", "/search", api.search)
    route("GET", r"/tracks/([A-Za-z0-9]+)", api.track)
    route("GET", r"/tracks/([A-Za-z0-9]+)/versions", api.versions)
    route("POST", r"/tracks/([A-Za-z0-9]+)/replace", api.replace)
    route("GET", r"/albums/([A-Za-z0-9]+)", api.album)
    route("GET", "/artist", api.artist)
    route("GET", "/artist/about", api.artist_about)
    route("GET", r"/lyrics/([A-Za-z0-9]+)", api.lyrics)
    route("GET", "/library", api.library)
    route("PUT", "/library/order", api.set_playlist_order)
    route("POST", "/library/liked/move", api.move_in_liked)
    route("GET", "/library/liked", api.liked)
    route("POST", "/playlists", api.create_playlist)
    route("GET", r"/playlists/([A-Za-z0-9]+)", api.playlist)
    route("PATCH", r"/playlists/(up[0-9a-f]+)", api.update_playlist)
    route("DELETE", r"/playlists/(up[0-9a-f]+)", api.delete_playlist)
    route("POST", r"/playlists/(up[0-9a-f]+)/tracks", api.add_to_playlist)
    route("DELETE", r"/playlists/(up[0-9a-f]+)/tracks/(\d+)", api.remove_from_playlist)
    route("POST", r"/playlists/(up[0-9a-f]+)/tracks/move", api.move_in_playlist)
    route("PUT", r"/likes/([A-Za-z0-9]+)", api.like)
    route("DELETE", r"/likes/([A-Za-z0-9]+)", api.unlike)
    route("POST", "/plays", api.play)
    route("GET", "/catalog/search", api.catalog_search)
    route("POST", "/requests", api.request)
    route("GET", "/catalog/albums", api.catalog_albums)
    route("GET", r"/catalog/albums/(deezer)/(\d+)", api.catalog_album)
    route("POST", "/requests/album", api.request_album)
    route("GET", "/requests", api.requests)
    route("GET", "/discover/genres", api.genres)
    route("GET", r"/discover/genres/(\d+)", api.genre)
    route("GET", "/podcasts", api.podcasts_home)
    route("GET", "/podcasts/search", api.podcast_search)
    route("GET", r"/podcasts/(ap\d+)", api.podcast)
    route("PUT", r"/podcasts/(ap\d+)/follow", api.follow_podcast)
    route("DELETE", r"/podcasts/(ap\d+)/follow", api.unfollow_podcast)
    route("POST", r"/podcasts/episodes/(ep[0-9a-f]{16})/progress", api.episode_progress)
    route("PUT", r"/podcasts/(ap\d+)/downloads", api.set_podcast_downloads)
    route("PUT", r"/podcasts/episodes/(ep[0-9a-f]{16})/download", api.keep_episode)
    route("DELETE", r"/podcasts/episodes/(ep[0-9a-f]{16})/download", api.drop_episode)
    route("GET", "/podcasts/downloads", api.podcast_downloads)
    route("GET", "/app/update", api.app_update)
    route("GET", "/people", api.people)
    route("POST", "/jams", api.start_jam)
    route("GET", "/jams/current", api.current_jam)
    route("GET", r"/jams/([0-9a-f]{12})", api.jam)
    route("POST", r"/jams/([0-9a-f]{12})/join", api.join_jam)
    route("POST", r"/jams/([0-9a-f]{12})/decline", api.decline_jam)
    route("POST", r"/jams/([0-9a-f]{12})/leave", api.leave_jam)
    route("POST", r"/jams/([0-9a-f]{12})/invite", api.invite_to_jam)
    route("POST", r"/jams/([0-9a-f]{12})/control", api.control_jam)
    route("POST", "/devices/push", api.push_token)
    route("GET", "/account", api.account)
    route("GET", "/artists/following", api.following_artists)
    route("POST", "/artists/follow", api.follow_artist)
    route("POST", "/artists/unfollow", api.unfollow_artist)
    route("GET", "/releases", api.new_releases)
    route("GET", "/notifications", api.notifications)
    route("POST", "/notifications/read", api.read_notifications)
    route("POST", "/account/password", api.set_password)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:
            return "Songarr"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("app %s %s", self.client_ip(), fmt % args)

        def client_ip(self) -> str:
            # Through Cloudflare Tunnel every connection comes from cloudflared on this PC;
            # Cloudflare passes the real address in CF-Connecting-IP.
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def send_json(self, status: int, obj: Any) -> None:
            data = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def not_found(self) -> None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def token(self) -> str:
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.lower().startswith("bearer ") else ""

        def body(self) -> dict:
            self._body_read = True
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "Request too large.")
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                raise ApiError(400, "Body must be JSON.") from None
            if not isinstance(data, dict):
                raise ApiError(400, "Body must be a JSON object.")
            return data

        def handle_any(self) -> None:
            self._body_read = False
            try:
                url = urlparse(self.path)
                path = url.path
                qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
                if path == "/download" or path.startswith("/download/"):
                    return self.download(path, qs)
                if not path.startswith("/api/v1/"):
                    return self.not_found()
                if svc.standby and path.startswith("/api/v1/"):  # a backup that isn't active right now
                    cluster = svc.cluster
                    return self.send_json(503, {"error": "This server is standing by; the app switches to the active one.",
                                                "standby": True, "active": cluster.active_url() if cluster else None,
                                                "servers": cluster.servers() if cluster else []})
                if path == "/api/v1/auth/login" and self.command == "POST":
                    return self.sign_in()
                if path == "/api/v1/auth/pair" and self.command == "POST":
                    return self.pair()
                user = api.users.authenticate(self.token(), self.client_ip())
                svc.last_app_request = time.time()
                if user is None:
                    return self.send_json(401, {"error": "This device isn't signed in. Sign in again with a pairing code or your password."})
                if path == "/api/v1/auth/logout" and self.command == "POST":
                    api.users.revoke_token(self.token())
                    return self.send_json(200, {"ok": True})
                if m := re.fullmatch(r"/api/v1/stream/([A-Za-z0-9]+)", path):
                    if self.command in ("GET", "HEAD"):
                        return self.stream(m[1])
                if m := re.fullmatch(r"/api/v1/podcasts/episodes/(ep[0-9a-f]{16})/stream", path):
                    if self.command in ("GET", "HEAD"):
                        return self.episode_stream(m[1])
                if path == "/api/v1/preview" and self.command in ("GET", "HEAD"):
                    return self.preview(qs)
                if path == "/api/v1/branding/icon" and self.command in ("GET", "HEAD"):
                    icon = svc.branding.png(int(qs.get("size") or 192) if (qs.get("size") or "192").isdigit() else 192)
                    return self.send_file(icon) if icon else self.send_json(404, {"error": "No icon has been chosen."})
                if path == "/api/v1/app/update/apk" and self.command in ("GET", "HEAD"):
                    apk = svc.app_updates.apk(qs.get("abi") or "")
                    return self.send_file(apk) if apk else self.send_json(404, {"error": "No update for this phone."})
                for method, rx, fn, _ in routes:
                    if method == self.command and (m := rx.match(path)):
                        body = self.body() if self.command in ("POST", "PUT", "PATCH") else {}
                        return self.send_json(200, fn(user, qs, body, *(unquote(g) for g in m.groups())))
                return self.send_json(404, {"error": "Unknown endpoint."})
            except (ApiError, JamError) as e:
                self.send_json(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                log.exception("app API %s %s failed", self.command, self.path)
                self.send_json(500, {"error": "Server error."})
            finally:
                svc.db.release()
                if not self._body_read and int(self.headers.get("Content-Length") or 0):
                    self.close_connection = True  # unread body would corrupt the next request on this connection

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any

        def download(self, path: str, qs: dict) -> None:
            """The app's download page (download_page.py), its pictures and the app; an empty 404 when
            it's switched off, like every other page."""
            if self.command not in ("GET", "HEAD") or not download_page.enabled(svc):
                return self.not_found()
            if path in ("/download", "/download/"):
                data = download_page.render(svc)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in download_page.HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            if path == "/download/icon.png":
                icon = svc.branding.png(192)
                return self.send_file(icon) if icon else self.not_found()
            if m := re.fullmatch(r"/download/img/([a-z0-9-]+)\.webp", path):
                shot = download_page.picture(m[1])
                return self.send_file(shot) if shot else self.not_found()
            if re.fullmatch(r"/download/[A-Za-z0-9._-]+\.apk", path):
                abi = qs.get("abi") or download_page.PHONE
                apk = svc.app_updates.apk(abi)
                if apk is None:
                    return self.not_found()
                name = download_page.apk_name(svc, abi)
                return self.send_file(apk, {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.not_found()

        def pair(self) -> None:
            body = self.body()
            got = api.users.pair(str(body.get("code") or ""), str(body.get("device") or "App"), self.client_ip())
            if got is None:
                return self.send_json(401, {"error": f"That code is wrong or expired. Make a new one in {svc.branding.name()}."})
            token, uid = got
            name = svc.db.one("SELECT name FROM users WHERE id = ?", (uid,))[0]
            self.send_json(200, {"token": token, "user": {"id": uid, "name": name}})

        def sign_in(self) -> None:
            body = self.body()
            try:
                token, uid = api.users.sign_in(str(body.get("login") or ""), str(body.get("password") or ""),
                                               str(body.get("device") or "App"), self.client_ip())
            except SignInError as e:
                return self.send_json(e.status, {"error": str(e)})
            name = svc.db.one("SELECT name FROM users WHERE id = ?", (uid,))[0]
            svc.db.log("people", f"{name} signed in with a password ({str(body.get('device') or 'App')[:60]})")
            self.send_json(200, {"token": token, "user": {"id": uid, "name": name}})

        def preview(self, qs: dict) -> None:
            """A 30-second preview of a song that isn't on the server yet (from Deezer)."""
            source, ext_id = qs.get("source"), qs.get("id") or ""
            try:
                url = discover.preview_url(int(ext_id) if source == "deezer" and ext_id.isdigit() else None,
                                           qs.get("isrc"), (qs.get("artist") or "")[:200], (qs.get("title") or "")[:300])
            except (OSError, RuntimeError, ValueError) as e:
                log.info("preview lookup failed: %s", e)
                return self.send_json(502, {"error": "Couldn't find a preview right now."})
            if not url:
                return self.send_json(404, {"error": "There's no preview for this song."})
            self.proxy(url, "audio/mpeg")

        def episode_stream(self, eid: str) -> None:
            """Pass an episode's audio through from the podcast's host (ranges included)."""
            row = svc.db.one("SELECT audio_url, audio_type FROM podcast_episodes WHERE id = ?", (eid,))
            if row is None:
                return self.send_json(404, {"error": "No such episode."})
            f = svc.db.one("SELECT path FROM episode_files WHERE episode_id = ?", (eid,))
            if f is not None and Path(f["path"]).is_file():
                return self.send_file(Path(f["path"]))
            self.proxy(row["audio_url"], row["audio_type"] or "audio/mpeg")

        def proxy(self, url: str, fallback_type: str) -> None:
            """Stream outside audio to the app, passing ranges through, so phones only talk to Songarr."""
            fwd = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
            try:
                up = podcasts.open_url(url, fwd, method=self.command, timeout=30)
            except urllib.error.HTTPError as e:
                with e:
                    if e.code == 416:
                        self.send_response(416)
                        if cr := e.headers.get("Content-Range"):
                            self.send_header("Content-Range", cr)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self.send_json(502, {"error": f"The audio's server answered {e.code}."})
            except (podcasts.PodcastError, OSError) as e:
                log.info("proxy %s: %s", url[:120], e)
                return self.send_json(502, {"error": "Couldn't reach the audio's server."})
            with up:
                self.send_response(up.status)
                ctype = up.headers.get("Content-Type") or ""
                if not ctype.startswith(("audio/", "video/")):
                    ctype = fallback_type
                self.send_header("Content-Type", ctype)
                for h in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                    if v := up.headers.get(h):
                        self.send_header(h, v)
                self.send_header("Cache-Control", "private, max-age=3600")
                if not up.headers.get("Content-Length"):
                    self.close_connection = True  # length unknown: the end of the body is the end of the connection
                    self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return
                while chunk := up.read(CHUNK):
                    self.wfile.write(chunk)

        def stream(self, tid: str) -> None:
            row = svc.db.one("SELECT file_path, status FROM tracks WHERE id = ?", (tid,))
            if row is None or row["status"] != "downloaded" or not row["file_path"]:
                return self.send_json(404, {"error": "That song isn't downloaded yet."})
            path = Path(row["file_path"])
            if not path.is_file():  # file deleted or moved: download it again
                svc.db.set_status(tid, "wanted", file_path=None, error="File was missing; downloading again")
                svc.wake.set()
                return self.send_json(404, {"error": "The file went missing; it's being downloaded again."})
            self.send_file(path)

        def send_file(self, path: Path, headers: dict | None = None) -> None:
            """A file from disk, with ranges and revalidation (and any extra [headers])."""
            try:
                st = path.stat()
            except OSError:
                return self.send_json(404, {"error": "That file is missing."})
            size = st.st_size
            etag = f'"{size:x}-{int(st.st_mtime):x}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end, status = 0, size - 1, 200
            if rng := self.headers.get("Range"):
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (m[1] == "" and m[2] == ""):
                    m = None
                elif m[1] == "":
                    start = max(0, size - int(m[2]))
                else:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                if m is None or start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, max-age=86400")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
