"""Podcasts: the Apple Podcasts directory for finding shows, each show's public RSS feed for episodes.

Episode audio is fetched by the server and passed through to the app, so phones only ever talk to
Songarr.

Feeds and episode links come from third parties, so every outside request goes through
`open_url`, which refuses addresses on this PC or the home network (and redirects to them).
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from .db import DB

log = logging.getLogger(__name__)

ITUNES = "https://itunes.apple.com"
COUNTRY = "us"
UA = "Songarr/0.1 (personal music server)"
FEED_MAX_BYTES = 20 * 1024 * 1024
FEED_MAX_AGE = 30 * 60          # re-read a show's feed at most every half hour
MAX_EPISODES = 300
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "content": "http://purl.org/rss/1.0/modules/content/"}

# Tests serve feeds from 127.0.0.1; real use never allows private addresses.
allow_private_hosts = False


class PodcastError(Exception):
    pass


class UnsafeURL(PodcastError):
    pass


# -- fetching -----------------------------------------------------------------------------

def check_url(url: str) -> None:
    """Refuse anything but http(s) to a public internet address."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL(f"Not a web address: {url[:100]}")
    if allow_private_hosts:
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise PodcastError(f"Can't find {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise UnsafeURL(f"{u.hostname} points into a private network")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects())


def open_url(url: str, headers: dict | None = None, method: str = "GET", timeout: float = 30):
    """urlopen for outside addresses only (redirects are checked too). Raises HTTPError as usual."""
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, method=method)
    return _opener.open(req, timeout=timeout)


_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _get_json(url: str, ttl: float) -> Any:
    with _cache_lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    with open_url(url, timeout=20) as r:
        data = json.loads(r.read(5 * 1024 * 1024))
    with _cache_lock:
        _cache[url] = (time.time(), data)
    return data


# -- the Apple Podcasts directory ----------------------------------------------------------

def _big_art(url: str | None) -> str | None:
    # Apple artwork URLs carry their size; ask for a sharper one.
    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png|webp)$", r"/600x600bb.\2", url) if url else url


def _from_itunes(r: dict) -> dict:
    return {
        "id": f"ap{r['collectionId']}",
        "title": r.get("collectionName") or r.get("trackName") or "",
        "author": r.get("artistName"),
        "artwork_url": r.get("artworkUrl600") or _big_art(r.get("artworkUrl100")),
        "feed_url": r.get("feedUrl"),
        "genre": r.get("primaryGenreName"),
        "genre_ids": [g for g in r.get("genreIds") or [] if g != "26"],  # 26 is just "Podcasts"
    }


def search(term: str, limit: int = 20) -> list[dict]:
    q = urllib.parse.urlencode({"media": "podcast", "term": term, "limit": min(50, limit), "country": COUNTRY})
    data = _get_json(f"{ITUNES}/search?{q}", 3600)
    return [_from_itunes(r) for r in data.get("results", []) if r.get("collectionId") and r.get("feedUrl")]


def lookup(apple_id: int) -> dict | None:
    data = _get_json(f"{ITUNES}/lookup?id={int(apple_id)}&entity=podcast", 24 * 3600)
    return next((_from_itunes(r) for r in data.get("results", []) if r.get("collectionId")), None)


def top(genre_id: str | None = None, limit: int = 25) -> list[dict]:
    """Apple's top podcasts chart, overall or for one genre (e.g. '1318' Technology)."""
    path = f"limit={min(100, limit)}" + (f"/genre={int(genre_id)}" if genre_id else "")
    data = _get_json(f"{ITUNES}/{COUNTRY}/rss/toppodcasts/{path}/json", 6 * 3600)
    entries = (data.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        try:
            apple_id = e["id"]["attributes"]["im:id"]
        except (KeyError, TypeError):
            continue
        images = e.get("im:image") or []
        cat = (e.get("category") or {}).get("attributes") or {}
        out.append({
            "id": f"ap{apple_id}",
            "title": (e.get("im:name") or {}).get("label") or "",
            "author": (e.get("im:artist") or {}).get("label"),
            "artwork_url": _big_art(images[-1]["label"]) if images else None,
            "feed_url": None,
            "genre": cat.get("label"),
            "genre_ids": [cat["im:id"]] if cat.get("im:id") else [],
            "description": ((e.get("summary") or {}).get("label") or "")[:1000] or None,
        })
    return out


# -- RSS feeds -------------------------------------------------------------------------

def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def plain(s: str, limit: int = 4000) -> str:
    """Show notes are HTML; keep readable text with paragraph breaks."""
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"(?i)<br\s*/?>|</li\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()[:limit]


def parse_duration(v: str) -> int | None:
    """itunes:duration is seconds or [H:]MM:SS. Returns milliseconds."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        if ":" in v:
            secs = 0
            for part in v.split(":"):
                secs = secs * 60 + int(float(part))
            return secs * 1000
        return int(float(v)) * 1000
    except ValueError:
        return None


def parse_feed(data: bytes, podcast_id: str) -> tuple[dict, list[dict]]:
    """(show info, episodes newest first) from an RSS feed."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise PodcastError(f"The podcast's feed isn't valid RSS ({e})") from None
    channel = root.find("channel")
    if channel is None:
        raise PodcastError("The podcast's feed has no channel")
    image = channel.find("itunes:image", NS)
    show = {
        "title": _text(channel.find("title")),
        "author": _text(channel.find("itunes:author", NS)) or None,
        "description": plain(_text(channel.find("description")) or _text(channel.find("itunes:summary", NS)), 2000) or None,
        "website": _text(channel.find("link")) or None,
        "artwork_url": (image.get("href") if image is not None else None) or _text(channel.find("image/url")) or None,
    }
    episodes = []
    seen = set()
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        url = (enc.get("url") or "").strip() if enc is not None else ""
        if not url.startswith(("http://", "https://")):
            continue  # not an audio episode (or a broken one)
        guid = _text(item.find("guid")) or url
        eid = "ep" + hashlib.sha1(f"{podcast_id}\n{guid}".encode()).hexdigest()[:16]
        if eid in seen:
            continue
        seen.add(eid)
        published = None
        if pub := _text(item.find("pubDate")):
            try:
                published = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError, IndexError):
                published = None
        img = item.find("itunes:image", NS)
        notes = _text(item.find("content:encoded", NS)) or _text(item.find("description")) or _text(item.find("itunes:summary", NS))
        episodes.append({
            "id": eid,
            "guid": guid[:500],
            "title": _text(item.find("title")) or "Untitled episode",
            "description": plain(notes) or None,
            "published": published,
            "duration_ms": parse_duration(_text(item.find("itunes:duration", NS))),
            "audio_url": url,
            "audio_type": (enc.get("type") or "").strip() or None,
            "image_url": img.get("href") if img is not None else None,
        })
    episodes.sort(key=lambda e: e["published"] or 0, reverse=True)
    return show, episodes[:MAX_EPISODES]


# -- storage -----------------------------------------------------------------------------

class Podcasts:
    def __init__(self, db: DB):
        self.db = db
        self._feed_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def upsert(self, shows: list[dict]) -> None:
        with self.db.tx() as c:
            for p in shows:
                c.execute("""INSERT INTO podcasts(id, title, author, artwork_url, feed_url, genre, genre_ids, description)
                             VALUES (?,?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET title = excluded.title,
                               author = COALESCE(excluded.author, podcasts.author),
                               artwork_url = COALESCE(excluded.artwork_url, podcasts.artwork_url),
                               feed_url = COALESCE(excluded.feed_url, podcasts.feed_url),
                               genre = COALESCE(excluded.genre, podcasts.genre),
                               genre_ids = CASE WHEN excluded.genre_ids = '[]' THEN podcasts.genre_ids ELSE excluded.genre_ids END,
                               description = COALESCE(podcasts.description, excluded.description)""",
                          (p["id"], p["title"], p.get("author"), p.get("artwork_url"), p.get("feed_url"), p.get("genre"),
                           json.dumps(p.get("genre_ids") or []), p.get("description")))

    def get(self, podcast_id: str) -> dict | None:
        """The show, looked up in the directory if Songarr hasn't seen it before."""
        row = self.db.one("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
        if row is None or not row["feed_url"]:
            if not re.fullmatch(r"ap\d+", podcast_id):
                return None
            found = lookup(int(podcast_id[2:]))
            if found is None or not found.get("feed_url"):
                return dict(row) if row else None
            self.upsert([found])
            row = self.db.one("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
        return dict(row)

    def refresh(self, podcast_id: str, max_age: float = FEED_MAX_AGE) -> dict:
        """Read the show's feed if it's older than max_age. Returns the show row."""
        with self._locks_lock:
            lock = self._feed_locks.setdefault(podcast_id, threading.Lock())
        with lock:  # two phones opening the same show read its feed once
            show = self.get(podcast_id)
            if show is None:
                raise PodcastError("No such podcast.")
            if show["fetched"] and time.time() - show["fetched"] < max_age:
                return show
            if not show["feed_url"]:
                raise PodcastError("This podcast has no public feed.")
            with open_url(show["feed_url"], timeout=30) as r:
                data = r.read(FEED_MAX_BYTES + 1)
            if len(data) > FEED_MAX_BYTES:
                raise PodcastError("The podcast's feed is too large.")
            info, episodes = parse_feed(data, podcast_id)
            with self.db.tx() as c:
                c.execute("""UPDATE podcasts SET description = COALESCE(?, description), website = COALESCE(?, website),
                             artwork_url = COALESCE(artwork_url, ?), author = COALESCE(author, ?), fetched = ? WHERE id = ?""",
                          (info["description"], info["website"], info["artwork_url"], info["author"], time.time(), podcast_id))
                c.executemany("""INSERT INTO podcast_episodes(id, podcast_id, guid, title, description, published, duration_ms,
                                                              audio_url, audio_type, image_url)
                                 VALUES (?,?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(id) DO UPDATE SET title = excluded.title, description = excluded.description,
                                   published = excluded.published, duration_ms = COALESCE(excluded.duration_ms, duration_ms),
                                   audio_url = excluded.audio_url, audio_type = excluded.audio_type, image_url = excluded.image_url""",
                              [(e["id"], podcast_id, e["guid"], e["title"], e["description"], e["published"], e["duration_ms"],
                                e["audio_url"], e["audio_type"], e["image_url"]) for e in episodes])
                keep = [e["id"] for e in episodes]
                if keep:  # episodes that dropped out of the feed go, unless someone is partway through one
                    c.execute(f"""DELETE FROM podcast_episodes WHERE podcast_id = ? AND id NOT IN ({','.join('?' * len(keep))})
                                  AND id NOT IN (SELECT episode_id FROM episode_progress WHERE completed = 0)
                                  AND id NOT IN (SELECT episode_id FROM episode_files)
                                  AND id NOT IN (SELECT episode_id FROM episode_holds WHERE released IS NULL)""",
                              [podcast_id, *keep])
            return dict(self.db.one("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)))

    # -- following -------------------------------------------------------------

    def followed_ids(self, user_id: int) -> list[str]:
        """The shows a person follows, the most recently followed first."""
        return [r[0] for r in self.db.q("SELECT podcast_id FROM podcast_follows WHERE user_id = ? ORDER BY created DESC",
                                        (user_id,))]

    def follow(self, user_id: int, podcast_id: str) -> None:
        if self.get(podcast_id) is None:
            raise PodcastError("No such podcast.")
        self.db.run("INSERT OR IGNORE INTO podcast_follows(user_id, podcast_id, created) VALUES (?,?,?)",
                    (user_id, podcast_id, time.time()))

    def unfollow(self, user_id: int, podcast_id: str) -> None:
        self.db.run("DELETE FROM podcast_follows WHERE user_id = ? AND podcast_id = ?", (user_id, podcast_id))

    # -- listening ---------------------------------------------------------------

    def set_progress(self, user_id: int, episode_id: str, position_ms: int, duration_ms: int | None, completed: bool) -> None:
        self.db.run("""INSERT INTO episode_progress(user_id, episode_id, position_ms, duration_ms, completed, updated)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(user_id, episode_id) DO UPDATE SET position_ms = excluded.position_ms,
                         duration_ms = COALESCE(excluded.duration_ms, duration_ms), completed = excluded.completed,
                         updated = excluded.updated""",
                    (user_id, episode_id, position_ms, duration_ms, int(completed), time.time()))
