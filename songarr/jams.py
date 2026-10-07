"""Jams: listening together. Everyone in a Jam hears the same song (or episode) at the same moment.

A Jam is a shared timeline: the queue, which item is current, whether it's playing, and "at
server time `ref` the position is `position_ms`". Phones turn that into a position using their
estimate of the server's clock, and correct themselves if they drift. Any member can play,
pause, seek, skip or add to the queue; each change bumps `version`, and phones waiting on
`wait()` (a long poll) hear about it at once. Starting playback is scheduled a moment ahead
(START_LEAD) so every phone has time to load the audio and begin together.

Jams live in memory: they're over when everyone leaves, or when nobody has checked in for a
couple of minutes (phones check in at least every 25 seconds).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Callable

START_LEAD = 1.2        # seconds between a "play" and everyone starting
SEEK_LEAD = 0.6
MEMBER_TIMEOUT = 120    # a phone that hasn't checked in for this long has left
INVITE_TIMEOUT = 15 * 60
MAX_QUEUE = 500
TOP_UP = 10  # songs added when a Jam's queue runs out
LOOP_MIN = 5  # a list at least this long goes round again when it runs out

log = logging.getLogger(__name__)


class JamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _window(queue: list[dict], index: int) -> tuple[list[dict], int]:
    """At most MAX_QUEUE items of a long list, from a little before [index]."""
    start = max(0, min(index - 50, len(queue) - MAX_QUEUE))
    queue = queue[start:start + MAX_QUEUE]
    return queue, max(0, min(index - start, len(queue) - 1))


class Jam:
    def __init__(self, host: int, queue: list[dict], index: int, position_ms: int, playing: bool):
        self.id = secrets.token_hex(6)
        self.host = host
        self.members: dict[int, float] = {host: time.time()}
        self.invited: dict[int, float] = {}
        self.queue, self.index = _window(queue, index)
        self.source = list(self.queue)  # the list it's playing from: goes round again when it runs out
        self.playing = playing
        self.position_ms = max(0, position_ms)
        self.ref = time.time() + (START_LEAD if playing else 0)
        self.version = 1
        self.created = time.time()
        self.ended = False

    def position_now(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        if not self.playing or now <= self.ref:
            return self.position_ms
        return self.position_ms + int((now - self.ref) * 1000)

    def state(self, names: dict[int, str]) -> dict:
        now = time.time()
        return {
            "id": self.id, "host": {"id": self.host, "name": names.get(self.host, "")},
            "members": [{"id": u, "name": names.get(u, "")} for u in self.members],
            "invited": [{"id": u, "name": names.get(u, "")} for u in self.invited],
            "queue": self.queue, "index": self.index, "playing": self.playing,
            "position_ms": self.position_ms, "ref": int(self.ref * 1000), "server_time": int(now * 1000),
            "version": self.version, "ended": self.ended,
        }


class Jams:
    def __init__(self) -> None:
        self._jams: dict[str, Jam] = {}
        self._cv = threading.Condition()
        # Picks songs to keep a Jam going when its queue runs out (set by the app API). Without
        # it, a Jam stops at the end of its queue.
        self.more: Callable[[Jam], list[dict]] | None = None

    def _top_up(self, jam: Jam) -> None:
        """Out of songs: play the list it's playing from again (or, for a short list, more songs
        the people in the Jam like). A Jam of podcast episodes just ends."""
        last = jam.queue[jam.index] if 0 <= jam.index < len(jam.queue) else {}
        songs = [t for t in jam.source if t.get("kind") != "episode"]
        if last.get("kind") == "episode":
            return
        if len(songs) >= LOOP_MIN:
            extra = [dict(t) for t in songs]
        elif self.more is None:
            return
        else:
            try:
                extra = self.more(jam)
            except Exception:  # a problem picking songs mustn't break the Jam
                log.exception("couldn't pick more songs for Jam %s", jam.id)
                return
        queue = jam.queue + extra
        drop = min(max(0, len(queue) - MAX_QUEUE), jam.index)  # forget the oldest played songs if it's full
        jam.queue, jam.index = queue[drop:][:MAX_QUEUE], jam.index - drop

    # -- lookup ---------------------------------------------------------------------------

    def _tidy(self) -> None:
        now = time.time()
        for jam in list(self._jams.values()):
            for u, seen in list(jam.members.items()):
                if now - seen > MEMBER_TIMEOUT:
                    self._drop(jam, u)
            for u, at in list(jam.invited.items()):
                if now - at > INVITE_TIMEOUT:
                    del jam.invited[u]
            if jam.ended:
                self._jams.pop(jam.id, None)

    def _drop(self, jam: Jam, user_id: int) -> None:
        jam.members.pop(user_id, None)
        if not jam.members:
            jam.ended = True
        elif jam.host == user_id:
            jam.host = next(iter(jam.members))  # someone still listening keeps it going
        jam.version += 1
        self._cv.notify_all()

    def get(self, jam_id: str, user_id: int) -> Jam:
        with self._cv:
            self._tidy()
            jam = self._jams.get(jam_id)
            if jam is None or (user_id not in jam.members and user_id not in jam.invited):
                raise JamError(404, "That Jam has ended.")
            return jam

    def count(self) -> int:
        """Jams going on right now."""
        with self._cv:
            self._tidy()
            return len(self._jams)

    def current(self, user_id: int) -> tuple[Jam | None, list[Jam]]:
        """(the Jam this profile is in, Jams it's invited to)."""
        with self._cv:
            self._tidy()
            mine = next((j for j in self._jams.values() if user_id in j.members), None)
            invites = [j for j in self._jams.values() if user_id in j.invited and j is not mine]
            return mine, invites

    # -- membership -------------------------------------------------------------------------

    def start(self, host: int, queue: list[dict], index: int, position_ms: int, playing: bool, invite: list[int]) -> Jam:
        if not queue:
            raise JamError(400, "Pick something to play first.")
        with self._cv:
            self._tidy()
            for jam in list(self._jams.values()):  # one Jam at a time per profile
                if host in jam.members:
                    self._drop(jam, host)
            jam = Jam(host, queue, index, position_ms, playing)
            jam.invited = {u: time.time() for u in invite if u != host}
            self._jams[jam.id] = jam
            return jam

    def invite(self, jam_id: str, user_id: int, others: list[int]) -> Jam:
        with self._cv:
            jam = self._member_jam(jam_id, user_id)
            for u in others:
                if u not in jam.members:
                    jam.invited[u] = time.time()
            jam.version += 1
            self._cv.notify_all()
            return jam

    def join(self, jam_id: str, user_id: int) -> Jam:
        with self._cv:
            self._tidy()
            jam = self._jams.get(jam_id)
            if jam is None or jam.ended:
                raise JamError(404, "That Jam has ended.")
            if user_id not in jam.invited and user_id not in jam.members:
                raise JamError(403, "You weren't invited to that Jam.")
            for other in list(self._jams.values()):  # leave any other Jam first
                if other is not jam and user_id in other.members:
                    self._drop(other, user_id)
            jam.invited.pop(user_id, None)
            jam.members[user_id] = time.time()
            jam.version += 1
            self._cv.notify_all()
            return jam

    def decline(self, jam_id: str, user_id: int) -> None:
        with self._cv:
            jam = self._jams.get(jam_id)
            if jam and jam.invited.pop(user_id, None) is not None:
                jam.version += 1
                self._cv.notify_all()

    def leave(self, jam_id: str, user_id: int) -> None:
        with self._cv:
            jam = self._jams.get(jam_id)
            if jam and user_id in jam.members:
                self._drop(jam, user_id)
                self._tidy()

    def _member_jam(self, jam_id: str, user_id: int) -> Jam:
        self._tidy()
        jam = self._jams.get(jam_id)
        if jam is None or jam.ended:
            raise JamError(404, "That Jam has ended.")
        if user_id not in jam.members:
            raise JamError(403, "Join the Jam first.")
        jam.members[user_id] = time.time()
        return jam

    # -- playback ---------------------------------------------------------------------------

    def control(self, jam_id: str, user_id: int, action: str, body: dict, items: list[dict] | None = None) -> Jam:
        """play / pause / seek / next / prev / jump / add / replace."""
        with self._cv:
            jam = self._member_jam(jam_id, user_id)
            now = time.time()
            pos = jam.position_now(now)
            if action == "play":
                jam.playing, jam.position_ms, jam.ref = True, int(body.get("position_ms", pos)), now + START_LEAD
            elif action == "pause":
                jam.playing, jam.position_ms, jam.ref = False, int(body.get("position_ms", pos)), now
            elif action == "seek":
                jam.position_ms = max(0, int(body.get("position_ms") or 0))
                jam.ref = now + (SEEK_LEAD if jam.playing else 0)
            elif action in ("next", "prev", "jump"):
                expected = body.get("expected_index")
                if expected is not None and int(expected) != jam.index:
                    return jam  # someone else already moved on (e.g. two phones reached the end together)
                if action == "next" and jam.index + 1 >= len(jam.queue):
                    self._top_up(jam)  # out of songs: keep the music going
                target = jam.index + 1 if action == "next" else jam.index - 1 if action == "prev" else int(body.get("index", 0))
                if not 0 <= target < len(jam.queue):
                    if action == "next":  # end of the queue: stop at the end
                        jam.playing, jam.position_ms, jam.ref = False, pos, now
                    else:
                        return jam
                else:
                    jam.index, jam.position_ms, jam.ref = target, 0, now + (START_LEAD if jam.playing else 0)
            elif action == "add":
                at = jam.index + 1 if body.get("next") else len(jam.queue)  # "play next" or the end
                jam.queue = (jam.queue[:at] + (items or []) + jam.queue[at:])[:MAX_QUEUE]
            elif action == "replace":
                if not items:
                    raise JamError(400, "Nothing to play.")
                jam.queue, jam.index = _window(items, int(body.get("index") or 0))
                jam.source = list(jam.queue)  # someone picked from another list: carry on from that one
                jam.position_ms, jam.playing, jam.ref = 0, True, now + START_LEAD
            else:
                raise JamError(400, f"Unknown action {action!r}.")
            jam.version += 1
            self._cv.notify_all()
            return jam

    def wait(self, jam_id: str, user_id: int, version: int, timeout: float = 25.0) -> Jam:
        """Return once the Jam differs from `version` (or after `timeout`): a long poll."""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                jam = self._jams.get(jam_id)
                if jam is None:
                    raise JamError(404, "That Jam has ended.")
                if user_id in jam.members:
                    jam.members[user_id] = time.time()
                elif user_id not in jam.invited:
                    raise JamError(404, "You're no longer in that Jam.")
                left = deadline - time.time()
                if jam.version != version or jam.ended or left <= 0:
                    return jam
                self._cv.wait(timeout=min(left, 5))
