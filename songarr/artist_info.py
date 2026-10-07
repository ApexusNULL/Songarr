"""Artist bios: a summary, quick facts and a short history, from Wikipedia and Wikidata.

The article is found among Wikipedia search results: the page whose title is the artist's name
(ignoring "(band)", "(rapper)", ...) and whose description says it's a musician or group, so
"Queen" finds the band and "violin" finds nothing. Text is Wikipedia's (CC BY-SA): the app
shows where it came from.

Wikimedia allows anonymous clients 10 requests a minute, or 200 when the User-Agent carries
contact details (the optional `wikimedia_contact` setting: an email or URL). Each bio takes
four requests, so bios are cached for 30 days (misses for 3), fetched one at a time by a
single worker, and prefetched in the background for the artists people are likely to open.
An app asking for an artist that isn't cached yet gets {"pending": true} and asks again.
"""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from .matching import norm

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

WIKI = "https://en.wikipedia.org"
WIKIDATA = "https://www.wikidata.org"
LASTFM = "https://ws.audioscrobbler.com/2.0/"
KEEP = 30 * 86400
KEEP_MISS = 3 * 86400
STARTUP_DELAY = 60  # seconds before background prefetching starts; tests raise it

MUSIC_WORDS = re.compile(
    r"\b(band|musician|singer|rapper|songwriter|group|duo|trio|quartet|dj|disc jockey|producer|composer|vocalist|"
    r"orchestra|ensemble|guitarist|pianist|drummer|bassist|violinist|cellist|conductor|artist|rock|pop|hip hop|"
    r"metal|punk|jazz|country|electronic|r&b|soul|folk|idol|boy band|girl group)\b", re.I)
HISTORY = re.compile(r"\b(history|career|biography|life)\b", re.I)
NOT_HISTORY = re.compile(r"\b(discography|legacy|influences|musical style|artistry|personal life|filmography|awards?)\b", re.I)
EARLY = re.compile(r"^(early (life|years)|early life and education|origins|background)$", re.I)
YEARS = re.compile(r"^\d{4}\s*[–-]")  # "1975–1983: …" chapters
MAX_CHAPTERS = 10
CHAPTER_CHARS = 520


class Busy(OSError):
    """Wikimedia asked us to slow down; try again later."""


class _Client:
    """Paced, identified requests to Wikimedia."""

    def __init__(self) -> None:
        self.contact = ""
        self.lastfm_key = ""
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked_until = 0.0

    @property
    def gap(self) -> float:
        return 0.35 if self.contact else 6.5  # stay under 200 or 10 requests a minute

    @property
    def user_agent(self) -> str:
        who = f"; {self.contact}" if self.contact else ""
        return f"Songarr/0.1 (self-hosted personal music server{who}) Python-urllib"

    def get(self, url: str) -> Any:
        with self._lock:
            if time.time() < self.blocked_until:
                raise Busy(f"Wikipedia asked us to wait until {time.strftime('%H:%M:%S', time.localtime(self.blocked_until))}")
            time.sleep(max(0.0, self._last + self.gap - time.time()))
            self._last = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read(8 * 1024 * 1024))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        wait = int(e.headers.get("Retry-After") or 60)
                    except ValueError:
                        wait = 60
                    self.blocked_until = time.time() + max(10, wait)
                    raise Busy("Wikipedia is rate-limiting requests") from None
                raise


client = _Client()


def _api(params: dict) -> Any:
    return client.get(f"{WIKI}/w/api.php?" + urllib.parse.urlencode({**params, "format": "json", "formatversion": 2}))


def _bare(title: str) -> str:
    """'Joji (musician)' -> 'joji'."""
    return norm(re.sub(r"\s*\([^)]*\)\s*$", "", title))


def find_page(name: str) -> dict | None:
    """The Wikipedia page about this artist: {title, description} or None."""
    want = norm(name)
    if not want:
        return None
    found = _api({"action": "query", "generator": "search", "gsrsearch": f"{name} music", "gsrlimit": 8,
                  "prop": "description|pageprops", "ppprop": "disambiguation"})
    pages = sorted(found.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for p in pages:
        if "disambiguation" in (p.get("pageprops") or {}):
            continue
        same = _bare(p["title"]) == want or _bare(p["title"]).replace(" ", "") == want.replace(" ", "")
        if same and MUSIC_WORDS.search(p.get("description") or ""):
            return {"title": p["title"], "description": p.get("description")}
    return None


def _sentences(text: str, limit: int) -> str:
    """Up to `limit` characters, ending at a sentence break."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit * 0.4 else cut.rsplit(" ", 1)[0] + "…").strip()


def sections(extract: str) -> list[tuple[int, str, str]]:
    """Plain article text -> [(level, heading, text)]; the lead is (1, '', text)."""
    out: list[tuple[int, str, str]] = []
    level, heading, buf = 1, "", []
    for line in extract.splitlines():
        m = re.fullmatch(r"(={2,6})\s*(.+?)\s*\1", line.strip())
        if m:
            out.append((level, heading, "\n".join(buf).strip()))
            level, heading, buf = len(m[1]), m[2], []
        else:
            buf.append(line)
    out.append((level, heading, "\n".join(buf).strip()))
    return out


def history(extract: str) -> list[dict]:
    """The article's life/career story as chapters: [{heading, text}], oldest first.

    Uses early-life sections, the history/career sections (their sub-sections become the
    chapters, e.g. "2009–2011: Selfish Machines"), and top-level chapters titled by years."""
    chapters: list[dict] = []
    seen: set[str] = set()

    def add(heading: str, text: str) -> None:
        paras = [p for p in text.split("\n\n") if p.strip()]
        if not paras or heading in seen:
            return
        first = paras[0] if len(paras[0]) >= 200 or len(paras) == 1 else paras[0] + " " + paras[1]
        seen.add(heading)
        chapters.append({"heading": heading, "text": _sentences(first, CHAPTER_CHARS)})

    secs = sections(extract)
    for i, (level, heading, text) in enumerate(secs):
        if level != 2:
            continue
        story = EARLY.match(heading) or YEARS.match(heading) or (HISTORY.search(heading) and not NOT_HISTORY.search(heading))
        if not story:
            continue
        subs = []
        for level2, heading2, text2 in secs[i + 1:]:
            if level2 <= 2:
                break
            if level2 == 3:
                # a chapter's own sub-sections (level 4) count as part of it
                subs.append([heading2, text2])
            elif subs and not subs[-1][1].strip():
                subs[-1][1] = text2
        if text.strip() and (not subs or len(text) > 300):
            add(heading, text)
        for h, t in subs:
            add(h, t)
    return chapters[:MAX_CHAPTERS]


# -- Wikidata facts -----------------------------------------------------------------------------

def _claims(entity: dict, prop: str) -> list:
    return [c["mainsnak"]["datavalue"]["value"] for c in entity.get("claims", {}).get(prop, [])
            if c.get("mainsnak", {}).get("datavalue") and c.get("rank") != "deprecated"]


def _year(values: list) -> str | None:
    for v in values:
        m = re.match(r"[+-]?(\d{4})", v.get("time", "") if isinstance(v, dict) else "")
        if m:
            return m[1]
    return None


def facts(qid: str) -> dict:
    data = client.get(f"{WIKIDATA}/wiki/Special:EntityData/{qid}.json")
    entity = data.get("entities", {}).get(qid) or next(iter(data.get("entities", {}).values()), {})

    def ids(prop: str, n: int) -> list[str]:
        return [v["id"] for v in _claims(entity, prop) if isinstance(v, dict) and "id" in v][:n]

    is_person = "Q5" in ids("P31", 10)
    want = {
        "origin": ids("P740", 1) or ids("P19", 1),
        "country": ids("P495", 1) or ids("P27", 1),
        "genres": ids("P136", 4),
        "labels": ids("P264", 3),
        "members": [] if is_person else ids("P527", 8),
    }
    all_ids = sorted({i for v in want.values() for i in v})
    labels: dict[str, str] = {}
    if all_ids:
        got = client.get(f"{WIKIDATA}/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": "|".join(all_ids[:50]), "props": "labels", "languages": "en", "format": "json"}))
        labels = {k: v.get("labels", {}).get("en", {}).get("value") for k, v in got.get("entities", {}).items()}
    named = {k: [labels[i] for i in v if labels.get(i)] for k, v in want.items()}
    origin = ", ".join(x for x in (named["origin"][:1] + named["country"][:1]) if x)
    if is_person:
        out = {"type": "person", "born": _year(_claims(entity, "P569")), "died": _year(_claims(entity, "P570"))}
    else:
        out = {"type": "group", "formed": _year(_claims(entity, "P571")) or _year(_claims(entity, "P2031")),
               "ended": _year(_claims(entity, "P576")) or _year(_claims(entity, "P2032"))}
    out |= {"origin": origin or None, "genres": named["genres"], "labels": named["labels"], "members": named["members"]}
    return {k: v for k, v in out.items() if v}


_lastfm_lock = threading.Lock()
_lastfm_last = 0.0


def lastfm(name: str, key: str) -> dict | None:
    """Fallback for artists Wikipedia doesn't cover: Last.fm's (user-written) bio and tags."""
    global _lastfm_last
    with _lastfm_lock:  # Last.fm asks for no more than about 5 requests a second
        time.sleep(max(0.0, _lastfm_last + 0.25 - time.time()))
        _lastfm_last = time.time()
    url = LASTFM + "?" + urllib.parse.urlencode({"method": "artist.getinfo", "artist": name, "api_key": key, "format": "json",
                                                 "autocorrect": 1, "lang": "en"})
    req = urllib.request.Request(url, headers={"User-Agent": client.user_agent})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read(2 * 1024 * 1024))
    a = data.get("artist")
    if not a or norm(a.get("name") or "") != norm(name):
        return None

    def clean(html_text: str) -> str:
        text = re.sub(r"<a [^>]*>Read more on Last\.fm</a>\.?", "", html_text or "")
        text = re.sub(r"User-contributed text is available under.*$", "", text, flags=re.S)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"[ \t]+", " ", text).strip()

    summary, content = clean((a.get("bio") or {}).get("summary")), clean((a.get("bio") or {}).get("content"))
    if not summary and not content:
        return None
    paras = [p.strip() for p in content.split("\n") if len(p.strip()) > 60]
    if paras and summary and norm(paras[0][:80]) == norm(summary[:80]):
        paras = paras[1:]  # the opening paragraph is the summary already shown
    tags = [t["name"] for t in ((a.get("tags") or {}).get("tag") or []) if t.get("name")][:4]
    return {
        "name": name,
        "title": a.get("name") or name,
        "description": None,
        "summary": _sentences(summary or content, 1200),
        "history": [{"heading": "", "text": _sentences(p, CHAPTER_CHARS)} for p in paras[:MAX_CHAPTERS]],
        "image_url": None,  # Last.fm no longer serves artist photos; the app uses Deezer's
        "url": a.get("url"),
        "source": "Last.fm",
        "license": "CC BY-SA 3.0",
        "facts": {"genres": tags} if tags else {},
    }


def fetch(name: str) -> dict | None:
    """Everything about an artist (Wikipedia, else Last.fm when a key is set), or None."""
    page = find_page(name)
    if page is None:
        if client.lastfm_key:
            try:
                return lastfm(name, client.lastfm_key)
            except (OSError, ValueError) as e:
                log.info("Last.fm bio for %r unavailable: %s", name, e)
        return None
    title = page["title"]
    # One request for the whole article: text, description, picture, link and Wikidata id.
    got = _api({"action": "query", "titles": title, "redirects": 1, "prop": "extracts|description|pageimages|pageprops|info",
                "explaintext": 1, "exsectionformat": "wiki", "piprop": "original|thumbnail", "pithumbsize": 800,
                "ppprop": "wikibase_item", "inprop": "url"})
    p = (got.get("query", {}).get("pages") or [{}])[0]
    text = p.get("extract") or ""
    info = {
        "name": name,
        "title": p.get("title") or title,
        "description": p.get("description") or page.get("description"),
        "summary": _sentences(sections(text)[0][2] if text else "", 1200),
        "history": history(text),
        "image_url": (p.get("original") or p.get("thumbnail") or {}).get("source"),
        "url": p.get("fullurl") or f"{WIKI}/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
        "source": "Wikipedia",
        "license": "CC BY-SA 4.0",
        "facts": {},
    }
    if qid := (p.get("pageprops") or {}).get("wikibase_item"):
        try:
            info["facts"] = facts(qid)
        except Busy:
            raise
        except (OSError, ValueError, KeyError) as e:
            log.info("Wikidata facts for %s unavailable: %s", title, e)
    return info


# -- cache, worker and prefetching -------------------------------------------------------------------

class ArtistInfo:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self.db = svc.db
        self._queue: list[tuple[int, int, str]] = []  # (priority, order, name): 0 = someone is waiting
        self._queued: dict[str, threading.Event] = {}
        self._order = itertools.count()
        self._cv = threading.Condition()

    def cached(self, name: str) -> tuple[bool, dict | None]:
        """(fresh in the cache?, data)."""
        row = self.db.one("SELECT data, fetched FROM artist_info WHERE name_key = ?", (norm(name),))
        if row is None:
            return False, None
        data = json.loads(row["data"])
        return time.time() - row["fetched"] < (KEEP if data else KEEP_MISS), data

    def about(self, name: str, wait: float = 6.0) -> tuple[dict | None, bool]:
        """(bio or None, still being fetched?). Waits a few seconds for an uncached artist."""
        if not norm(name):
            return None, False
        fresh, data = self.cached(name)
        if fresh:
            return data, False
        done = self.request(name, priority=0)
        done.wait(wait)
        fresh, data2 = self.cached(name)
        return (data2 if fresh else data), not fresh

    def wake(self) -> None:
        """Interrupt the worker's wait (used when Songarr stops)."""
        with self._cv:
            self._cv.notify_all()

    def request(self, name: str, priority: int = 1) -> threading.Event:
        key = norm(name)
        with self._cv:
            if key in self._queued:
                if priority == 0:  # someone is waiting now: move it to the front
                    heapq.heappush(self._queue, (0, next(self._order), name))
                return self._queued[key]
            ev = self._queued[key] = threading.Event()
            heapq.heappush(self._queue, (priority, next(self._order), name))
            self._cv.notify()
            return ev

    def run(self) -> None:
        """The one worker that talks to Wikimedia: requests first, then prefetching."""
        stop = self.svc.stop_event
        started = time.time()
        while not stop.is_set():
            client.contact = (self.db.setting("wikimedia_contact") or "").strip()
            client.lastfm_key = (self.db.setting("lastfm_api_key") or "").strip()
            with self._cv:
                if not self._queue and time.time() - started > STARTUP_DELAY:
                    for name in self._prefetch_candidates(5):
                        key = norm(name)
                        if key not in self._queued:
                            self._queued[key] = threading.Event()
                            heapq.heappush(self._queue, (1, next(self._order), name))
                if not self._queue:
                    if not stop.is_set():
                        self._cv.wait(timeout=60 if time.time() - started > STARTUP_DELAY else 5)
                    continue
                _, _, name = heapq.heappop(self._queue)
                ev = self._queued.get(norm(name))
            if ev is None or ev.is_set():
                continue
            wait = client.blocked_until - time.time()
            if wait > 0:
                stop.wait(wait)
            try:
                if not self.cached(name)[0]:
                    data = fetch(name)
                    self.db.run("INSERT OR REPLACE INTO artist_info(name_key, data, fetched) VALUES (?,?,?)",
                                (norm(name), json.dumps(data), time.time()))
            except Busy as e:
                log.info("artist bios paused: %s", e)
                with self._cv:  # try again once Wikipedia is ready
                    heapq.heappush(self._queue, (1, next(self._order), name))
                continue
            except (OSError, ValueError, KeyError) as e:
                log.info("artist bio for %r unavailable: %s", name, e)
            with self._cv:
                self._queued.pop(norm(name), None)
            ev.set()
        self.db.release()

    def _prefetch_candidates(self, n: int) -> list[str]:
        """Artists people are likely to open: recommended and favourite ones, then the library's biggest."""
        names: list[str] = []
        for (data,) in self.db.q("SELECT data FROM recommendations"):
            recs = json.loads(data)
            names += [a["name"] for a in recs.get("top_artists", []) + recs.get("artists", [])]
        counts: dict[str, int] = {}
        for (arts,) in self.db.q("SELECT artists FROM tracks WHERE status = 'downloaded'"):
            for a in json.loads(arts)[:1]:
                counts[a] = counts.get(a, 0) + 1
        names += sorted(counts, key=counts.get, reverse=True)
        have = {r[0] for r in self.db.q("SELECT name_key FROM artist_info")}
        out, seen = [], set()
        for name in names:
            key = norm(name)
            if key and key not in have and key not in seen:
                seen.add(key)
                out.append(name)
                if len(out) >= n:
                    break
        return out
