"""A backup server: two (or more) Songarr servers sharing the music on a NAS, one active at a time.

Say this Windows PC is the main server and an Ubuntu box the backup. Both reach the NAS: the music
folder (X:\\Music here, /mnt/nas/Music there) and a shared "cluster folder" beside it.

- lease.json in the cluster folder names the active server, which rewrites it every few seconds
  (its heartbeat).
- The active server copies its database and a few files (the Firebase key, YouTube cookies, the
  name and icon, app updates) into snapshot/ whenever something changed, at most every two minutes.
- A standby server keeps its own copy in step, translating paths as it goes: the music, podcast
  and data folders are wherever they are on that server.
- If the heartbeat stops for 45 seconds, the standby takes the lease and restarts as the active
  server: downloading, answering the apps (which know every server's address and
  switch by themselves). What changed on the old server after its last copy is lost.
- A server set up as the main one asks for the lease back when it returns, and the active backup
  hands over once nobody is listening ("Make this server active" does it straight away). Stopping an
  active server on purpose hands over to a standby first, so nothing is lost.
- Only the server holding the lease writes; one that finds the lease taken restarts as a standby,
  and one that can't renew it for a while stops taking changes (they'd be lost) until it can.
  Telling servers apart needs no clock: a heartbeat is a counter that keeps changing.

Each server keeps a few settings to itself (cluster.json in its data folder): its music and podcast
folders, FFmpeg, its own app address, and so on.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .selfupdate import RUNNING

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

TICK = 5  # seconds between heartbeats
TAKEOVER_AFTER = 45  # a heartbeat that hasn't changed for this long: the active server is gone
FENCE_AFTER = 25  # an active server that couldn't renew the lease for this long stops taking changes
SNAPSHOT_EVERY = 120
HANDOVER_LONGEST = 6 * 3600  # hand back to the main server by then even if someone keeps listening
SETTLE = 10 * 60  # a server that just took over stays active at least this long (no flip-flopping)
KEEP_SNAPSHOTS = 3
TAKE_PAUSE = 2.0  # after writing the lease: long enough for a server writing at the same moment to show
REPLACE_TRIES = 30  # × 0.1 s: how long a write waits for a reader to let go
MAILBOX_KEEP = 600  # a request or answer nobody collected in this long (by this server's own clock) goes
# settings each server keeps to itself
NODE_LOCAL = ("library_root", "podcast_root", "ffmpeg_path", "public_url", "app_source_dir", "flutter_path", "auto_update")
MIRRORED = ("firebase-service-account.json", "branding", "app-updates")  # plus the YouTube cookies file
ROLES = ("main", "backup")


class ClusterError(ValueError):
    pass


def _read_json(path: Path) -> dict | None:
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:  # being replaced this very moment (Windows)
            time.sleep(0.05)
        except (OSError, ValueError):
            return None
    return None


def _replace(src: Path, dst: Path) -> None:
    """os.replace, waiting a moment while someone reads [dst]: Windows can't replace a file another
    program has open (the admin page or another server reading it), even on the NAS."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.1)


def _write_json(path: Path, data: dict) -> None:
    """Write then rename, so a reader never sees half a file (raises OSError if the folder is gone)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    try:
        _replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# -- paths: X:\Music\Artist on one server is /mnt/nas/Music/Artist on another --------------------------

_WINDOWS = re.compile(r"^(?:[A-Za-z]:|[\\/]{2})")


def _windows(p: str) -> bool:
    return bool(_WINDOWS.match(p or ""))


def translate(path: str, maps: list[tuple[str, str]]) -> str:
    """[path] if it's inside one of the maps' source folders, moved to that map's folder here."""
    if not path:
        return path
    slashed = path.replace("\\", "/")
    for src, dst in sorted(maps, key=lambda m: -len(m[0])):
        if not src or not dst:
            continue
        root = src.replace("\\", "/").rstrip("/")
        head = slashed[:len(root)]
        same = head.lower() == root.lower() if _windows(src) else head == root
        if same and (len(slashed) == len(root) or slashed[len(root)] == "/"):
            parts = [p for p in slashed[len(root):].split("/") if p]
            if _windows(dst):
                return "\\".join([dst.replace("/", "\\").rstrip("\\"), *parts])
            return "/".join([dst.rstrip("/"), *parts])
    return path


def translate_db(path: Path, source: dict, here: dict) -> int:
    """Move every stored path in the database at [path] from [source]'s folders to [here]'s."""
    maps = [(source.get(k) or "", here.get(k) or "") for k in ("library_root", "podcast_root", "data_dir")]
    maps = [(s, d) for s, d in maps if s and d and translate(s, [(d, d)]) != d]
    if not maps:
        return 0
    moved = 0
    con = sqlite3.connect(path)
    try:
        with con:
            for table, col, key in (("tracks", "file_path", "id"), ("library_files", "path", "path"),
                                    ("episode_files", "path", "episode_id"), ("playlists", "m3u_path", "id")):
                try:
                    rows = con.execute(f"SELECT {key}, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
                except sqlite3.OperationalError:  # an older server without this table
                    continue
                changes = [(translate(v, maps), k) for k, v in rows if translate(v, maps) != v]
                con.executemany(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", changes)
                moved += len(changes)
            for key in ("cookies_file",):
                row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                if row and isinstance(value := json.loads(row[0]), str) and (new := translate(value, maps)) != value:
                    con.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(new), key))
                    moved += 1
    finally:
        con.close()
    return moved


# -- this server's part ------------------------------------------------------------------------------

class ClusterConfig:
    """cluster.json in the data folder: the shared folder, this server's name and role, and the
    settings it keeps to itself."""

    FILE = "cluster.json"

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / self.FILE
        data = _read_json(self.path) or {}
        self.folder: str = data.get("folder") or ""
        self.name: str = data.get("name") or ""
        self.role: str = data.get("role") if data.get("role") in ROLES else "main"
        self.local: dict[str, Any] = data.get("local") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.folder and self.name)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(self.path, {"folder": self.folder, "name": self.name, "role": self.role, "local": self.local})


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name or len(name) > 40 or not re.fullmatch(r"[\w .()'-]+", name):
        raise ClusterError("Give this server a short name (letters, numbers, spaces, - . ' ( )).")
    return name


class Cluster:
    def __init__(self, data_dir: Path, config: ClusterConfig):
        self.data_dir = Path(data_dir)
        self.config = config
        self.name = config.name
        self.folder = Path(config.folder)
        self.db_path = self.data_dir / "songarr.db"
        self.state = "standby"  # or "active"
        self.epoch = 0
        self.svc: Service | None = None
        self.wake = threading.Event()
        self.info_state: dict = {"snapshot": None, "imported_at": None, "error": None, "handover": None}
        self._beat = 0
        self._last_lease: tuple | None = None
        self._lease_changed = time.monotonic()
        self._peers: dict[str, tuple[int, float]] = {}  # name -> (their beat, when it last changed)
        self._renewed = time.monotonic()  # when this server last wrote the lease (while active)
        self._fenced = False  # active, but couldn't renew the lease: not taking changes meanwhile
        self._paused_before = False
        self._snap_conn: sqlite3.Connection | None = None
        self._snap_version: int | None = None  # the last snapshot this server made
        self._last_dv: int | None = None
        self._last_files: dict | None = None
        self._last_snapshot = 0.0
        self._mail_seen: dict[Path, float] = {}  # mailbox file -> when this server first saw it
        self._handover_seen: float | None = None
        self._asked = False
        self._chosen = False  # made active with "Make active": the main server doesn't ask for it back
        self._active_since = time.monotonic()
        self._done = False  # handed over or demoted: this process is on its way out
        self._lock = threading.Lock()
        self.imported = (_read_json(self.data_dir / "cluster-state.json") or {}).get("imported")

    lease_file = property(lambda self: self.folder / "lease.json")
    handover_file = property(lambda self: self.folder / "handover.json")
    nodes_dir = property(lambda self: self.folder / "nodes")
    snap_dir = property(lambda self: self.folder / "snapshot")

    def _read_lease(self) -> dict | None:
        """The lease; None if there's none, or it's damaged (an empty file after a power cut, say).
        Raises OSError if it can't be read (the NAS is away)."""
        for _ in range(5):
            try:
                text = self.lease_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                if self.folder.is_dir():  # (Windows says "not found" for a network path that's away, too)
                    return None
                raise
            except PermissionError:  # being replaced this very moment (Windows)
                time.sleep(0.05)
                continue
            try:
                lease = json.loads(text)
            except ValueError:
                return None
            return lease if isinstance(lease, dict) else None
        raise PermissionError(f"{self.lease_file} stays in use")

    # -- starting ---------------------------------------------------------------------------------

    def startup(self) -> str:
        """'active' when the lease is this server's (or nobody's yet and this is the main server),
        else 'standby'."""
        self.folder.mkdir(parents=True, exist_ok=True)
        lease = self._read_lease()
        if lease is None and not self.lease_file.exists() and self.config.role == "main":
            if self._take(1):  # the main server starts things off; a backup waits for it, so its data wins
                self.state = "active"
                return "active"
            lease = _read_json(self.lease_file)
        if lease and lease.get("node") == self.name:
            self.state, self.epoch = "active", int(lease.get("epoch") or 0)
            self._chosen = bool(lease.get("chosen"))
            self._active_since = time.monotonic()
            return "active"
        self.state = "standby"
        # the 45 seconds start now: a standby starting while the active server is already gone takes over then
        self._last_lease = (lease.get("node"), lease.get("epoch"), lease.get("beat")) if lease else None
        self._lease_changed = time.monotonic()
        return "standby"

    def attach(self, svc: Service) -> None:
        """This server's own settings win over the shared ones (and are saved to cluster.json)."""
        self.svc = svc
        svc.cluster = self
        svc.db.local_keys = NODE_LOCAL
        if self.imported is None:  # first time in a cluster: carry over what this server had
            carried = {key: value for key in NODE_LOCAL  # (after taking in a shared copy, the database
                       if key not in self.config.local  # holds another server's)
                       and (value := svc.db.setting(key)) not in (None, "")}
            if carried:
                self.config.local.update(carried)
                self.config.save()  # kept from now on, whatever database this server holds later
        svc.db.local = dict(self.config.local)
        svc.db.save_local = self._save_local
        if self.state == "standby":
            svc.standby = True

    def _save_local(self, values: dict) -> None:
        self.config.local = dict(values)
        self.config.save()

    # -- what everyone else needs to know ----------------------------------------------------------

    def roots(self) -> dict:
        """This server's folders, for translating paths."""
        local = self.config.local
        library = local.get("library_root") or (self.svc.db.setting("library_root") if self.svc else "") or ""
        podcasts = local.get("podcast_root") or (str(Path(library).parent / "Podcasts") if library else "")
        return {"library_root": str(library), "podcast_root": str(podcasts), "data_dir": str(self.data_dir)}

    def app_url(self) -> str:
        return str(self.config.local.get("public_url") or (self.svc.db.setting("public_url") if self.svc else "") or "")

    def nodes(self) -> list[dict]:
        """Every server in the cluster (from their heartbeats), the active one first."""
        lease = _read_json(self.lease_file) or {}
        out = []
        try:
            files = sorted(self.nodes_dir.glob("*.json"))
        except OSError:
            files = []
        for f in files:
            n = _read_json(f)
            if not n or not n.get("name"):
                continue
            beat, changed = self._peers.get(n["name"], (None, 0.0))
            alive = n["name"] == self.name or (beat is not None and time.monotonic() - changed < TAKEOVER_AFTER)
            out.append(n | {"alive": alive, "active": n["name"] == lease.get("node"), "me": n["name"] == self.name})
        return sorted(out, key=lambda n: (not n["active"], n["name"]))

    def servers(self) -> list[str]:
        """Every server's app address, the active one first: the apps switch between them."""
        urls: list[str] = []
        for n in self.nodes():
            if n.get("app_url") and n["app_url"] not in urls:
                urls.append(n["app_url"])
        return urls

    def active_url(self) -> str | None:
        lease = _read_json(self.lease_file) or {}
        return lease.get("app_url") or None

    def info(self) -> dict:
        lease = _read_json(self.lease_file) or {}
        return {"enabled": True, "name": self.name, "role": self.config.role, "folder": self.config.folder,
                "state": self.state, "active": lease.get("node"), "nodes": self.nodes()} | self.info_state

    # -- the heartbeat ------------------------------------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self._guard, name="cluster-guard", daemon=True).start()
        while not self._done:
            try:
                self.tick()
                self.info_state["error"] = None
            except OSError as e:  # the NAS is away: keep going, keep trying
                self.info_state["error"] = f"The cluster folder can't be reached: {e}"
                log.warning("cluster folder %s: %s", self.folder, e)
            except Exception:
                log.exception("cluster")
            finally:
                if self.svc:
                    self.svc.db.release()
            self.wake.wait(TICK)
            self.wake.clear()

    def tick(self) -> None:
        with self._lock:
            if self._done:
                return
            try:
                self._beat += 1
                self._write_node()
                self._watch_peers()
                if self.state == "active":
                    self._lead()
                else:
                    self._follow()
            except OSError:  # the NAS is away: only time spent watching an unchanged lease counts towards
                self._lease_changed = time.monotonic()  # taking over (once it's back, the active one beats again)
                raise

    def _guard(self) -> None:
        """(A thread of its own: the heartbeat may be stuck on the NAS.)"""
        while not self._done:
            time.sleep(1)
            self._fence_if_stale()

    def _fence_if_stale(self) -> None:
        """An active server that couldn't renew the lease for a while stops answering the apps and
        starting downloads: a standby may be taking over, and what this one took in meanwhile would be
        lost. It carries on once it renews the lease (or stands by, if another server has it now)."""
        if self.state != "active" or self._done or self._fenced or not self.svc:
            return
        if time.monotonic() - self._renewed > FENCE_AFTER:
            log.warning("couldn't renew the lease for %d seconds: not taking changes until it can", FENCE_AFTER)
            self._fenced, self._paused_before = True, self.svc.paused
            self.svc.standby = self.svc.paused = True

    def _write_node(self) -> None:
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.nodes_dir / f"{self.name}.json", {
            "name": self.name, "role": self.config.role, "state": self.state, "app_url": self.app_url(),
            "version": __version__ + (f"+{RUNNING[:7]}" if RUNNING else ""), "os": platform.system(), "beat": self._beat,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            # what any server's admin page shows and can change for this one
            "local": {k: self.config.local.get(k) or "" for k in ("library_root", "podcast_root", "public_url", "ffmpeg_path")}})
        if self._beat % 12 == 0:
            self._tidy_mailbox()

    def _tidy_mailbox(self) -> None:
        """Requests and answers nobody collected (a server that went away mid-way). Timed by this
        server's own clock, from when it first saw them: the NAS's and other servers' clocks may differ."""
        now, seen = time.monotonic(), {}
        for folder in (self.commands_dir, self.results_dir):
            for f in folder.glob("*") if folder.is_dir() else []:
                first = self._mail_seen.get(f, now)
                try:
                    if now - first >= MAILBOX_KEEP:
                        f.unlink()
                        continue
                except OSError:
                    pass
                seen[f] = first
        self._mail_seen = seen

    def _watch_peers(self) -> None:
        for f in self.nodes_dir.glob("*.json"):
            n = _read_json(f)
            if not n or n.get("name") in (None, self.name):
                continue
            beat, changed = self._peers.get(n["name"], (None, 0.0))
            if n.get("beat") != beat:
                self._peers[n["name"]] = (n.get("beat"), time.monotonic())

    def _peer_alive(self, name: str) -> bool:
        beat, changed = self._peers.get(name, (None, 0.0))
        return beat is not None and time.monotonic() - changed < TAKEOVER_AFTER

    # -- active -------------------------------------------------------------------------------------

    def _lead(self) -> None:
        lease = self._renew()
        if lease is not None:
            log.warning("%s has taken over; this server stands by now", lease.get("node"))
            if self.svc:
                self.svc.db.log("cluster", f"{lease.get('node')} took over as the active server; this one stands by now.")
            self._leave_and_restart()
            return
        if self._fenced:  # the lease is renewed again: back to work
            log.info("renewed the lease again: taking changes again")
            self._fenced = False
            if self.svc:
                self.svc.standby, self.svc.paused = False, self._paused_before
        if time.time() - self._last_snapshot >= SNAPSHOT_EVERY:
            self.snapshot()
        self._maybe_hand_over()

    def _renew(self) -> dict | None:
        """The heartbeat: write the lease again. Returns the lease instead if another server has it now."""
        lease = self._read_lease()
        if lease is not None and (lease.get("node") != self.name or int(lease.get("epoch") or 0) != self.epoch):
            return lease
        # (none, or a damaged one: still this server's, to write again)
        _write_json(self.lease_file, {"node": self.name, "epoch": self.epoch, "beat": self._beat, "chosen": self._chosen,
                                      "app_url": self.app_url(), "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self._renewed = time.monotonic()
        return None

    def _keep_beating(self) -> None:
        """The heartbeat during something long (a big snapshot to a slow NAS, stopping for a hand-over),
        or a standby would take over meanwhile."""
        if time.monotonic() - self._renewed < TICK:
            return
        self._beat += 1
        if (lease := self._renew()) is not None:
            raise ClusterError(f"{lease.get('node')} has the lease now")  # (the next heartbeat stands by)

    def _maybe_hand_over(self) -> None:
        h = _read_json(self.handover_file)
        if not h or h.get("to") in (None, self.name):
            self._handover_seen = None
            if h:
                self.handover_file.unlink(missing_ok=True)
            self.info_state["handover"] = None
            return
        to = h["to"]
        self._handover_seen = self._handover_seen or time.monotonic()
        self.info_state["handover"] = to
        if not self._peer_alive(to):
            return
        if h.get("now"):  # "Make active" on the other server
            self.hand_over(to, chosen=True)
            return
        if time.monotonic() - self._active_since < SETTLE:
            return  # just took over: let things settle first
        quiet = self.svc.dependencies.quiet() if self.svc else True
        if quiet or time.monotonic() - self._handover_seen >= HANDOVER_LONGEST:
            self.hand_over(to)

    def hand_over(self, to: str, restart: bool = True, chosen: bool = False) -> None:
        """Stop writing, take a last snapshot, and give the lease to [to] ([chosen]: with "Make active",
        so it stays active until someone chooses otherwise)."""
        log.info("handing over to %s", to)
        if self.svc:
            self.svc.db.log("cluster", f"Handed over to {to}.")
            self.svc.standby = True  # the apps get pointed elsewhere from now on
            # (finishing the downloads in progress can take minutes: the heartbeat goes on meanwhile, or
            # [to] would take over by itself, without this server's last copy)
            stopping = threading.Thread(target=self.svc.stop, kwargs={"wait": True}, name="stopping", daemon=True)
            stopping.start()
            while stopping.is_alive():
                stopping.join(1)
                self._keep_beating()
        self.snapshot(force=True)
        # [to] takes over once it has this last copy ("snapshot"), so nothing is lost
        _write_json(self.lease_file, {"node": to, "epoch": self.epoch + 1, "beat": 0, "app_url": "", "chosen": chosen,
                                      "snapshot": self._snap_version, "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                      "from": self.name})
        self.handover_file.unlink(missing_ok=True)
        self.state = "standby"
        if restart:
            self._leave_and_restart()
        else:
            self._done = True
            self.close()

    def on_exit(self) -> None:
        """Stopping on purpose: hand over to a standby that's there, so nothing is lost."""
        with self._lock:
            if self._done or self.state != "active":
                return
            for n in self.nodes():
                if not n["me"] and n.get("state") == "standby" and self._peer_alive(n["name"]):
                    try:
                        self.hand_over(n["name"], restart=False)
                    except (OSError, ClusterError) as e:
                        log.warning("couldn't hand over to %s: %s", n["name"], e)
                    return

    def close(self) -> None:
        """Let go of the database (the snapshot connection)."""
        if self._snap_conn is not None:
            self._snap_conn.close()
            self._snap_conn = None

    def _leave_and_restart(self) -> None:
        self._done = True
        self.close()
        self.state = "standby"
        if self.svc:
            self.svc.standby = True
            if self.svc.restart_hook:
                threading.Thread(target=self.svc.restart_hook, daemon=True).start()

    # -- snapshots ----------------------------------------------------------------------------------

    def _mirrored(self) -> dict[str, list]:
        """The data folder's files a standby needs, with their size and time."""
        out: dict[str, list] = {}
        items = list(MIRRORED)
        cookies = (self.svc.db.setting("cookies_file") if self.svc else "") or ""
        if cookies and Path(cookies).is_file() and Path(cookies).resolve().is_relative_to(self.data_dir.resolve()):
            items.append(str(Path(cookies).resolve().relative_to(self.data_dir.resolve())))
        for item in items:
            path = self.data_dir / item
            for f in ([path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []):
                if f.is_file() and not f.name.endswith(".part"):
                    st = f.stat()
                    out[f.relative_to(self.data_dir).as_posix()] = [st.st_size, int(st.st_mtime)]
        return out

    def snapshot(self, force: bool = False) -> bool:
        """Copy the database and files to the cluster folder if anything changed. True if copied."""
        if self._snap_conn is None:
            self._snap_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        dv = self._snap_conn.execute("PRAGMA data_version").fetchone()[0]
        files = self._mirrored()
        self._last_snapshot = time.time()
        if not force and dv == self._last_dv and files == self._last_files:
            return False
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        meta = _read_json(self.snap_dir / "meta.json") or {}
        # (never a number used before, even when meta.json can't be read: a standby that had it wouldn't copy it)
        version = max(int(meta.get("version") or 0), self._snap_version or 0, self.imported or 0) + 1
        work = Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.data_dir))
        try:
            copy = sqlite3.connect(work / "songarr.db")
            self._snap_conn.backup(copy)  # a consistent copy, even while Songarr writes
            copy.close()
            name = f"songarr-{version}.db.gz"
            with open(work / "songarr.db", "rb") as src, gzip.open(work / name, "wb", compresslevel=3) as dst:
                self._pour(src, dst)
            with open(work / name, "rb") as src, open(self.snap_dir / (name + ".part"), "wb") as dst:
                self._pour(src, dst)
            _replace(self.snap_dir / (name + ".part"), self.snap_dir / name)
            old = meta.get("files") or {}
            for rel, stamp in files.items():
                if old.get(rel) != stamp or not (self.snap_dir / "files" / rel).exists():
                    target = self.snap_dir / "files" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.data_dir / rel, "rb") as src, open(target.with_name(target.name + ".part"), "wb") as dst:
                        self._pour(src, dst)
                    _replace(target.with_name(target.name + ".part"), target)
            _write_json(self.snap_dir / "meta.json", {
                "version": version, "node": self.name, "epoch": self.epoch, "db": name, "roots": self.roots(),
                "files": files, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._last_dv, self._last_files, self._snap_version = dv, files, version
        self.info_state["snapshot"] = time.time()
        # the oldest go, by their times on the NAS; never the one just made (whose time may be the
        # oldest, if the clock of the server that made the others is ahead)
        older = sorted((p for p in self.snap_dir.glob("songarr-*.db.gz") if p.name != name), key=lambda p: p.stat().st_mtime)
        for f in older[:max(0, len(older) - (KEEP_SNAPSHOTS - 1))]:
            try:
                f.unlink()
            except OSError:
                pass  # a standby is reading it: next time
        return True

    def _pour(self, src, dst) -> None:
        """shutil.copyfileobj, with the heartbeat going on meanwhile (a big database, a slow NAS)."""
        while chunk := src.read(1 << 20):
            dst.write(chunk)
            self._keep_beating()

    # -- standby ------------------------------------------------------------------------------------

    def _follow(self) -> None:
        lease = self._read_lease()  # (none, or a damaged one that stays so: nobody's heartbeat either)
        key = (lease.get("node"), lease.get("epoch"), lease.get("beat")) if lease else None
        if key != self._last_lease:
            self._last_lease, self._lease_changed = key, time.monotonic()
        if lease and lease.get("node") == self.name:  # handed to this server
            self._promote(int(lease.get("epoch") or 0), f"{lease.get('from') or 'The active server'} handed over",
                          final=lease.get("snapshot"))
            return
        try:
            self.import_snapshot()
        except Exception as e:  # it mustn't keep this server from taking over with the copy it has
            log.warning("couldn't copy the latest snapshot: %s", e)
        if time.monotonic() - self._lease_changed >= TAKEOVER_AFTER:
            if self.config.role != "main" and self.imported is None:
                return  # no copy from the main server yet: a backup has nothing to take over with
            gone = (lease or {}).get("node") or "nobody"
            epoch = int((lease or {}).get("epoch") or 0) + 1
            if self._take(epoch):
                self._promote(epoch, f"{gone} went quiet")
            return
        if self.config.role == "main" and lease and not lease.get("chosen") and not self._asked:  # back again: ask for the lease
            _write_json(self.handover_file, {"to": self.name, "now": False, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
            self._asked = True

    def make_active(self) -> None:
        """'Make this server active': ask the active one to hand over now."""
        if self.state == "active":
            return
        _write_json(self.handover_file, {"to": self.name, "now": True, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self._asked = True
        self.wake.set()

    def _take(self, epoch: int) -> bool:
        _write_json(self.lease_file, {"node": self.name, "epoch": epoch, "beat": 0, "app_url": self.app_url(),
                                      "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        time.sleep(TAKE_PAUSE)  # another server may have written at the same moment: the file says who won
        lease = _read_json(self.lease_file) or {}
        if lease.get("node") == self.name and int(lease.get("epoch") or 0) == epoch:
            self.epoch = epoch
            return True
        return False

    def _promote(self, epoch: int, why: str, final: int | None = None) -> None:
        """[final]: handed over, with the last snapshot of the server that handed over."""
        try:
            self.import_snapshot()  # the latest copy first
        except Exception as e:
            log.warning("couldn't get the last snapshot: %s", e)
        if final is not None and self.imported != final:
            # Not without it: what was done since this server's copy would be lost. The other server
            # stands by with everything meanwhile; this one tries again with the next heartbeat. (If
            # it can't for a while, the lease, unchanged, goes back to the other one: nothing is lost.)
            return
        log.info("%s: this server takes over", why)
        self.epoch = epoch
        if self.svc:
            self.svc.db.log("cluster", f"{why}; this server ({self.name}) is now the active one.")
        self._leave_and_restart()  # starts again as the active server: the lease is ours

    def import_snapshot(self) -> bool:
        """Bring this server's copy up to date from the cluster folder. True if it changed."""
        meta = _read_json(self.snap_dir / "meta.json")
        if not meta or meta.get("version") == self.imported or meta.get("node") == self.name:
            return False
        work = Path(tempfile.mkdtemp(prefix="import-", dir=self.data_dir))
        try:
            with gzip.open(self.snap_dir / meta["db"], "rb") as src, open(work / "songarr.db", "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            con = sqlite3.connect(work / "songarr.db")
            try:
                ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                con.close()
            if not ok:
                raise OSError("the snapshot is damaged")
            moved = translate_db(work / "songarr.db", meta.get("roots") or {}, self.roots())
            if self.imported is None:
                self._keep_own_copy()  # joining: what this server had is kept, not lost
            for rel, stamp in (meta.get("files") or {}).items():
                target = self.data_dir / rel
                if not target.exists() or target.stat().st_size != stamp[0] or int(target.stat().st_mtime) != stamp[1]:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.snap_dir / "files" / rel, target.with_name(target.name + ".part"))
                    _replace(target.with_name(target.name + ".part"), target)
                    os.utime(target, (stamp[1], stamp[1]))
            self._swap(work / "songarr.db")
        except OSError:
            raise
        except Exception as e:  # a damaged copy (cut short, say), or one that won't translate: like the
            raise OSError(f"the snapshot can't be used: {e!r}") from e  # NAS being away, for whoever asked
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.imported = meta["version"]
        _write_json(self.data_dir / "cluster-state.json", {"imported": self.imported})
        self.info_state["imported_at"] = time.time()
        log.info("copied snapshot %s from %s (%d paths translated)", meta["version"], meta.get("node"), moved)
        return True

    # -- one admin page for every server -------------------------------------------------------------
    #
    # Servers talk through the cluster folder, so none has to open its admin website to the network:
    # a standby's admin page forwards what you do to the active server, and each server's own controls
    # (make it active, restart it, its folders) reach it wherever you are.

    commands_dir = property(lambda self: self.folder / "commands")
    results_dir = property(lambda self: self.folder / "results")

    def send(self, to: str, payload: dict, wait: float = 25.0) -> dict:
        """Ask server [to] ("active" for whichever is active) to do something; returns its answer."""
        cid = f"{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}-{os.urandom(3).hex()}"
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.commands_dir / f"{cid}.json", {"id": cid, "to": to, "from": self.name, "payload": payload})
        answer = self.results_dir / f"{cid}.json"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            result = _read_json(answer)
            if result is not None:
                answer.unlink(missing_ok=True)
                return result
            time.sleep(0.15)
        (self.commands_dir / f"{cid}.json").unlink(missing_ok=True)
        raise ClusterError(f"{'The active server' if to == 'active' else to} didn't answer.")

    def serve_commands(self) -> None:
        """Carry out what other servers' admin pages asked of this one (a quick loop of its own)."""
        while not self._done:
            try:
                for f in sorted(self.commands_dir.glob("*.json")) if self.commands_dir.is_dir() else []:
                    cmd = _read_json(f)
                    if not cmd or not (cmd.get("to") == self.name or (cmd.get("to") == "active" and self.state == "active")):
                        continue
                    claimed = f.with_name(f.name + f".{self.name}.taken")
                    try:
                        os.replace(f, claimed)  # only one server carries it out
                    except OSError:
                        continue
                    try:
                        result = self._carry_out(cmd.get("payload") or {})
                    except Exception as e:  # report it to whoever asked
                        log.exception("command from %s", cmd.get("from"))
                        result = {"status": 500, "error": str(e)}
                    try:
                        _write_json(self.results_dir / f"{cmd['id']}.json", result)
                    finally:
                        claimed.unlink(missing_ok=True)
                    after = result.pop("_then", None) if isinstance(result, dict) else None
                    if after:
                        after()
            except OSError:
                pass  # the NAS is away: the heartbeat loop reports it
            finally:
                if self.svc:
                    self.svc.db.release()
            time.sleep(0.3)

    def _carry_out(self, payload: dict) -> dict:
        kind = payload.get("kind")
        if kind == "http":  # an admin page request, made to this server's own admin website
            return self._local_http(payload)
        if kind == "restart":
            hook = self.svc.restart_hook if self.svc else None
            if hook is None:
                return {"status": 409, "error": f"{self.name} can't restart itself."}
            return {"status": 200, "ok": True, "_then": lambda: threading.Timer(0.8, hook).start()}  # after answering
        if kind == "make-active":
            self.make_active()
            return {"status": 200, "ok": True}
        if kind == "update-check":
            result = self.svc.dependencies.check(install=True) if self.svc else {}
            return {"status": 200} | result
        if kind == "settings":  # this server's own settings
            values = {k: v for k, v in (payload.get("values") or {}).items() if k in NODE_LOCAL}
            self.config.local.update(values)
            self.config.save()
            if self.svc:
                self.svc.db.local.update(values)
                self.svc.scanner.wake.set()
                self.svc.wake.set()
            return {"status": 200, "ok": True, "local": self.config.local}
        return {"status": 400, "error": f"unknown request {kind!r}"}

    def _local_http(self, payload: dict) -> dict:
        import base64
        import http.client

        port = getattr(self.svc, "port", None) if self.svc else None
        if port is None:
            return {"status": 503, "error": "This server's admin website isn't running."}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            headers = {"Host": f"127.0.0.1:{port}", **{k: v for k, v in (payload.get("headers") or {}).items()
                                                        if k.lower() in ("content-type", "x-songarr")}}
            body = base64.b64decode(payload["body"]) if payload.get("body") else None
            conn.request(payload.get("method") or "GET", payload.get("path") or "/", body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            return {"status": r.status, "type": r.getheader("Content-Type") or "application/json",
                    "location": r.getheader("Location"), "body": base64.b64encode(data).decode()}
        finally:
            conn.close()

    def forward(self, method: str, path: str, body: bytes | None, headers: dict) -> dict:
        """An admin page request made on this standby, carried out by the active server."""
        import base64

        return self.send("active", {"kind": "http", "method": method, "path": path, "headers": headers,
                                    "body": base64.b64encode(body).decode() if body else None})

    def command(self, to: str, kind: str, values: dict | None = None) -> dict:
        """One of a server's own controls, for this server or another."""
        payload = {"kind": kind, "values": values or {}}
        if to == self.name:
            result = self._carry_out(payload)
            after = result.pop("_then", None)
            if after:
                after()
            return result
        return self.send(to, payload)

    def _keep_own_copy(self) -> None:
        """The first time this server takes in the shared copy, keep its own database beside it
        (songarr-before-joining.db), in case it held anything the shared one doesn't."""
        keep = self.data_dir / "songarr-before-joining.db"
        if keep.exists() or not self.db_path.exists():
            return
        src = sqlite3.connect(self.db_path)
        try:
            dst = sqlite3.connect(keep)
            try:
                src.backup(dst)  # consistent even while the database is in use
            finally:
                dst.close()
        finally:
            src.close()
        log.info("kept this server's own database as %s", keep)

    def _swap(self, new_db: Path) -> None:
        """Put the new database in place of this server's (nothing may hold it open meanwhile)."""
        self.close()
        for attempt in range(20):
            if self.svc:
                self.svc.db.close()
            try:
                for ext in ("-wal", "-shm"):
                    self.db_path.with_name(self.db_path.name + ext).unlink(missing_ok=True)
                os.replace(new_db, self.db_path)
                return
            except PermissionError:  # a web request opened it again just now
                time.sleep(0.25)
        raise OSError("couldn't replace the database (it's in use)")
