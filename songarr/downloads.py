"""How the songs each person added from the app are getting on: the app's progress bars (GET
/requests/progress) and the phone notification that follows them through their stages.

A person's batch is everything they've asked for since the oldest of their songs still on its way, so
adding more while it runs makes it bigger, and it's over once nothing of theirs is waiting or
downloading. Songs that failed and are tried again later (an hour on), or that need a match picked
on the server, don't hold it open: they're counted as needing a hand.

The phone shows one silent notification per person, updated in place: "Finding it on YouTube",
"Downloading 12 of 547", "Paused: ...", and at the end "Your songs are ready". It goes out as a data
message that the app draws itself, with a progress bar (DownloadsReceiver.kt): straight away when a
batch starts, pauses or finishes, and in between at most every few seconds for a few songs, or once
a minute for a big batch. A finished batch of several songs also leaves a note in the bell.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

PENDING = ("wanted", "searching", "downloading")
_PENDING = "('wanted', 'searching', 'downloading')"
LOOK_EVERY = 5  # seconds, while anyone's songs are on their way
IDLE_LOOK = 120  # otherwise (a request wakes it sooner)
SMALL = 3  # a batch this size gets each stage as it happens...
EVERY_SMALL = 4
EVERY_BIG = 60  # ...a bigger one an update a minute at most
REQUEST_PRIORITY = 10  # (service.REQUEST_PRIORITY: requests download before everything else)


def _artists(r) -> list[str]:
    try:
        return json.loads(r["artists"] or "[]")
    except ValueError:
        return []


def stage_of(active: dict) -> tuple[str, float]:
    """A song being worked on, as one of: finding, waiting (YouTube pacing), downloading, finishing."""
    s = active.get("stage") or ""
    if s == "downloading":
        return "downloading", float(active.get("percent") or 0)
    if s.startswith("waiting"):
        return "waiting", 0.0
    if s in ("converting", "tagging", "moving to library", "copying the same recording"):
        return "finishing", 100.0
    return "finding", 0.0  # (queued, searching YouTube, checking a version picked by hand)


STAGE_WORDS = {"finding": "Finding it on YouTube", "waiting": "Waiting its turn with YouTube",
               "downloading": "Downloading", "finishing": "Almost ready"}


class Downloads:
    def __init__(self, svc: Service):
        self.svc = svc
        self.db = svc.db
        self.wake = threading.Event()
        self.sent: dict[int, dict] = {}  # person -> what their phone was last told
        self.batches: dict[int, float] = {}  # person -> when their batch began (it stays put as songs arrive)
        self.now = time.time  # (tests move the clock)

    # -- where things are ------------------------------------------------------------------------------

    def paused(self) -> dict | None:
        """Why nothing's downloading for anyone right now, if that's so."""
        svc = self.svc
        if svc.paused:
            return {"reason": "Downloads are paused on the server", "until": None}
        if svc.cooldown_until > self.now():
            return {"reason": "YouTube asked Songarr to slow down", "until": svc.cooldown_until}
        if not svc.scanner.ready.is_set():
            return {"reason": "Songarr is looking through the music folder first", "until": None}
        return None

    def progress(self, uid: int) -> dict | None:
        """This person's batch: how many songs, how many are ready, what's being done to which right now.
        None when nothing of theirs is on its way."""
        oldest = self.db.one(f"""SELECT MIN(r.created) FROM requests r JOIN tracks t ON t.id = r.track_id
                                 WHERE r.user_id = ? AND t.monitored = 1 AND t.status IN {_PENDING}""", (uid,))[0]
        if oldest is None:
            self.batches.pop(uid, None)
            return None
        start = self.batches[uid] = min(self.batches.get(uid, oldest), oldest)
        rows = self.db.q("""SELECT t.id, t.title, t.artists, t.thumb_url, t.cover_url, t.status, t.monitored, t.requested_at,
                                   t.next_attempt, t.updated, r.created
                            FROM requests r JOIN tracks t ON t.id = r.track_id
                            WHERE r.user_id = ? AND r.created >= ? ORDER BY r.created""", (uid, start))
        working = {a["id"]: a for a in self.svc.snapshot()}
        ready = waiting = later = stuck = 0
        active, queued, oldest = [], None, None
        for r in rows:
            if _already_there(r):
                continue
            if r["status"] == "downloaded":
                ready += 1
            elif not r["monitored"]:
                continue  # taken out of their library since: not theirs to wait for
            elif r["status"] in PENDING:
                oldest = min(oldest or r["requested_at"] or start, r["requested_at"] or start)
                if r["id"] in working:
                    stage, percent = stage_of(working[r["id"]])
                    active.append({"id": r["id"], "title": r["title"], "artists": _artists(r),
                                   "thumb_url": r["thumb_url"] or r["cover_url"], "stage": stage, "percent": round(percent, 1)})
                else:
                    waiting += 1
                    queued = r  # (the newest request goes first)
            elif r["status"] == "failed" and r["next_attempt"] is not None:
                later += 1
            else:
                stuck += 1  # needs a match picked, couldn't be found, or ignored on the server
        paused = self.paused()
        return {"total": ready + waiting + len(active) + later + stuck, "ready": ready, "waiting": waiting, "later": later,
                "stuck": stuck, "active": active, "paused": paused, "eta": self._eta(oldest, paused), "since": start,
                "next": {"title": queued["title"], "artists": _artists(queued)} if queued else None}

    def _eta(self, oldest: float | None, paused: dict | None) -> int | None:
        """Roughly how long until the last of them is in: the newest request goes first, so everything
        requested (by anyone) since their oldest one waiting is ahead of it, at YouTube's pace."""
        if oldest is None or (paused and paused["until"] is None):
            return None
        ahead = self.db.one(f"""SELECT COUNT(*) FROM tracks WHERE monitored = 1 AND status IN {_PENDING}
                                AND priority >= ? AND requested_at >= ?""", (REQUEST_PRIORITY, oldest))[0]
        per_hour = max(1, self.svc.throttle_info()["effective_per_hour"])
        wait = max(0.0, paused["until"] - self.now()) if paused else 0.0
        return int(ahead * 3600 / per_hour + wait)

    def finished(self, uid: int, since: float) -> dict:
        """The counts of a batch that's over."""
        ready = later = stuck = 0
        first = None
        for r in self.db.q("""SELECT t.title, t.artists, t.status, t.monitored, t.next_attempt, t.updated, r.created
                              FROM requests r JOIN tracks t ON t.id = r.track_id WHERE r.user_id = ? AND r.created >= ?""",
                           (uid, since)):
            if _already_there(r):
                continue
            if r["status"] == "downloaded":
                ready += 1
            elif not r["monitored"]:
                continue
            elif r["status"] == "failed" and r["next_attempt"] is not None:
                later += 1
            else:
                stuck += 1
            first = first or r
        return {"total": ready + later + stuck, "ready": ready, "later": later, "stuck": stuck,
                "title": first["title"] if first else None, "artists": _artists(first) if first else []}

    # -- the phone notification ------------------------------------------------------------------------

    @staticmethod
    def message(p: dict) -> dict:
        """What the notification says about a batch on its way: {stage, title, body, ready, total, indeterminate}."""
        total, ready, active = p["total"], p["ready"], p["active"]
        one = total == 1 and (active or p["waiting"])
        if p["paused"]:
            until = p["paused"]["until"]
            body = p["paused"]["reason"] + (f"; carrying on at {time.strftime('%H:%M', time.localtime(until))}" if until else "")
            title = f"Waiting to download {'1 song' if total == 1 else f'{total - ready} songs'}"
            return {"stage": "paused", "title": title, "body": body, "ready": ready, "total": total, "indeterminate": False}
        now = active[0] if active else None
        if one:
            song = now or p["next"] or {"title": "Your song", "artists": []}
            stage = now["stage"] if now else "queued"
            name = song["title"]
            title = {"finding": f"Finding {name} on YouTube", "waiting": f"{name}: waiting its turn with YouTube",
                     "queued": f"{name} is waiting to download", "downloading": f"Downloading {name}",
                     "finishing": f"{name} is almost ready"}[stage]
            body = ", ".join(song["artists"]) if now else "After the songs ahead of it"
            return {"stage": stage, "title": title, "body": body, "ready": int(now["percent"]) if stage == "downloading" else 0,
                    "total": 100, "indeterminate": stage != "downloading"}
        if not active:
            title = f"{total - ready} songs waiting to download"
        else:
            title = f"Downloading {total} songs" if ready == 0 else f"{ready} of {total} songs ready"
        bits = [f"Now: {now['title']}" if now else "Waiting for the songs ahead of them"]
        if p["eta"] and p["eta"] >= 120:
            bits.append(f"about {_duration(p['eta'])} left")
        return {"stage": "running", "title": title, "body": " · ".join(bits), "ready": ready, "total": total,
                "indeterminate": False}

    @staticmethod
    def done_message(f: dict) -> dict:
        total, ready, trouble = f["total"], f["ready"], f["later"] + f["stuck"]
        if total == 1 and f["title"]:
            if ready:
                return {"title": f"{f['title']} is ready", "body": ", ".join(f["artists"]) + " · on your server"}
            return {"title": f"{f['title']} couldn't be downloaded yet",
                    "body": "Songarr tries again later" if f["later"] else "It needs a hand on the server (Requested songs)"}
        title = "Your songs are ready" if not trouble else f"{ready} of {total} songs are ready"
        bits = [f"{ready} song{'' if ready == 1 else 's'} on your server"] if not trouble else []
        if f["later"]:
            bits.append(f"{f['later']} will be tried again later")
        if f["stuck"]:
            bits.append(f"{f['stuck']} need{'s' if f['stuck'] == 1 else ''} a hand (see Requested songs)")
        return {"title": title, "body": ", ".join(bits)}

    def check(self) -> None:
        """Look at everyone with songs on their way, and tell their phones what's changed."""
        users = {r[0] for r in self.db.q(f"""SELECT DISTINCT r.user_id FROM requests r JOIN tracks t ON t.id = r.track_id
                                              WHERE t.monitored = 1 AND t.status IN {_PENDING}""")}
        for uid in users | set(self.sent):
            last = self.sent.get(uid)
            p = self.progress(uid)
            if p is None:
                if last:
                    self._finish(uid, last)
                continue
            m = self.message(p)
            now = self.now()
            every = EVERY_SMALL if p["total"] <= SMALL else EVERY_BIG
            changed = last is None or m["stage"] != last["stage"] and (m["stage"] == "paused" or last["stage"] == "paused")
            moved = last is not None and (m["title"], m["ready"], m["total"]) != (last["title"], last["ready"], last["total"])
            if changed or moved and now - last["at"] >= every:
                self._send(uid, m | {"kind": "progress"}, urgent=changed)
                self.sent[uid] = m | {"at": now, "since": p["since"] if last is None else last["since"]}

    def _finish(self, uid: int, last: dict) -> None:
        del self.sent[uid]
        f = self.finished(uid, last["since"])
        if not f["total"]:
            self._send(uid, {"kind": "clear", "title": "", "body": ""}, urgent=True)  # (all taken out again)
            return
        m = self.done_message(f)
        self._send(uid, {"kind": "done", "stage": "done", **m, "ready": f["ready"], "total": f["total"]}, urgent=True)
        if f["total"] > 1:
            self.svc.releases.notify([uid], "downloads", m["title"], m["body"], {"requests": True})

    def _send(self, uid: int, m: dict, urgent: bool) -> None:
        data = {"type": "downloads", "kind": m["kind"], "stage": m.get("stage", ""), "title": m["title"], "body": m["body"],
                "ready": m.get("ready", 0), "total": m.get("total", 0), "indeterminate": int(bool(m.get("indeterminate"))),
                "at": int(self.now() * 1000)}  # (messages can arrive out of order: the phone keeps the newest)
        self.svc.push.send_data([uid], data, urgent=urgent, collapse="downloads", ttl=86400 if m["kind"] == "done" else 600)

    def run(self) -> None:
        while not self.svc.stop_event.is_set():
            self.wake.wait(timeout=LOOK_EVERY if self.sent else IDLE_LOOK)
            self.wake.clear()
            if self.svc.stop_event.is_set():
                break
            try:
                self.check()
            except Exception:
                log.exception("looking at downloads failed")
            finally:
                self.db.release()


def _already_there(r) -> bool:
    """On the server before it was asked for (it joined their library): nothing to download."""
    return r["status"] == "downloaded" and r["updated"] is not None and r["updated"] < r["created"]


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 10 else f"{hours:.1f} h".replace(".0 h", " h")
