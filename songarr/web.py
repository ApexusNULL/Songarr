"""Web UI + JSON API on 127.0.0.1.

Browsers let any website send requests to localhost, so every state-changing call needs
an `X-Songarr` header (a cross-site page can't add one without a CORS preflight, which
we never allow) and the Host header must be ours (blocks DNS-rebinding).
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import segno

from . import __version__, exportify
from .db import STATUSES, track_dict
from .branding import BrandError
from .cluster import NODE_LOCAL, ROLES, ClusterConfig, ClusterError, clean_name
from .service import Service
from .youtube import FORMATS, find_ffmpeg, find_js_runtime

log = logging.getLogger(__name__)
UI = Path(__file__).with_name("ui.html")
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|^)([A-Za-z0-9_-]{11})(?:[?&#/]|$)")

SETTING_RULES: dict[str, Any] = {
    "library_root": lambda v: str(v).strip(),
    "audio_format": lambda v: v if v in FORMATS else "m4a",
    "workers": lambda v: max(1, min(8, int(v))),
    "cookies_file": lambda v: str(v).strip().strip('"'),
    "match_threshold": lambda v: max(0.3, min(0.95, float(v))),
    "ffmpeg_path": lambda v: str(v).strip().strip('"'),
    "max_per_hour": lambda v: max(10, min(2000, int(v))),
    "write_playlist_files": lambda v: bool(v),
    "download_page": lambda v: bool(v),
    "public_url": lambda v: _public_url(v),
    "wikimedia_contact": lambda v: _contact(v),
    "lastfm_api_key": lambda v: _lastfm_key(v),
    "auto_update": lambda v: bool(v),
    "app_source_dir": lambda v: str(v).strip(),
    "flutter_path": lambda v: str(v).strip(),
}


def _ui(svc: Service) -> bytes:
    """The admin page, with the chosen name and icon in its title, favicon and header."""
    b = svc.branding.info()
    page = UI.read_text(encoding="utf-8").replace("<title>Songarr</title>", f"<title>{html.escape(b['name'])}</title>")
    if b["icon"]:
        page = re.sub(r'<link rel="icon" href="[^"]*">', f'<link rel="icon" href="/brand/icon-32.png?v={b["icon"]}">', page, count=1)
    page = page.replace("const BRAND = {};", "const BRAND = " + json.dumps(b).replace("<", "\\u003c") + ";", 1)
    here = _here(svc)
    return page.replace("const HERE = {};", "const HERE = " + json.dumps(here).replace("<", "\\u003c") + ";", 1).encode()


def _here(svc: Service) -> dict:
    """Which server this admin page is on (with backup servers): its name, and whether it's standing by."""
    c = svc.cluster
    if c is None:
        return {"cluster": False}
    try:
        active = c.info().get("active")
    except OSError:
        active = None
    return {"cluster": True, "name": c.name, "state": c.state, "active": active}


# Admin requests a standby carries out itself; everything else goes to the active server.
LOCAL_POSTS = ("/api/system/restart", "/api/system/shutdown", "/api/system/update-check")


def _forwarded(p: str, post: bool) -> bool:
    if p.startswith("/api/cluster") or (post and p in LOCAL_POSTS):
        return False
    return p.startswith("/api/")


def _lastfm_key(v: Any) -> str:
    v = str(v or "").strip()
    if v and not re.fullmatch(r"[0-9a-f]{32}", v):
        raise ValueError("A Last.fm API key is 32 letters and digits (0-9, a-f).")
    return v


def _contact(v: Any) -> str:
    v = str(v or "").strip()
    if v and not (re.fullmatch(r"[^@\s()]+@[^@\s()]+\.[^@\s()]+", v) or re.fullmatch(r"https?://[^\s()]+", v)):
        raise ValueError("The Wikipedia contact must be an email address or a web address.")
    return v


def _public_url(v: Any) -> str:
    v = str(v or "").strip().rstrip("/")
    if v and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?", v):
        raise ValueError("The app address must start with https:// (your Cloudflare Tunnel hostname).")
    return v


def youtube_id(text: str) -> str | None:
    m = _YT_ID.search(text.strip())
    return m[1] if m else None


def make_server(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Songarr/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- plumbing --------------------------------------------------------

        def _send(self, code: int, body: bytes | str, ctype: str = "application/json", headers: dict | None = None) -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str))

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, "Forbidden host", "text/plain")
                return False
            return True

        # -- backup servers (cluster.py) ------------------------------------------------------------

        def _forward(self, method: str, raw: bytes | None = None) -> None:
            """On a standby: the active server answers (through the cluster folder on the NAS)."""
            try:
                r = svc.cluster.forward(method, self.path, raw, {"Content-Type": self.headers.get("Content-Type") or "",
                                                                  "X-Songarr": self.headers.get("X-Songarr") or ""})
            except (ClusterError, OSError) as e:
                return self._json({"error": f"{e} This server is standing by, so the active one answers; "
                                            "try again in a moment."}, 503)
            if "body" not in r:  # the active server couldn't carry it out
                return self._json({"error": r.get("error") or "The active server couldn't do that."}, r.get("status") or 502)
            self._send(r.get("status") or 502, base64.b64decode(r["body"] or ""), r.get("type") or "application/json",
                       {"Location": r["location"]} if r.get("location") else None)

        def _cluster_info(self) -> dict:
            if svc.cluster:
                return svc.cluster.info()
            import socket
            return {"enabled": False, "suggested_name": socket.gethostname(), "roles": list(ROLES)}

        def _cluster_post(self, p: str, body: dict) -> None:
            if p == "/api/cluster/setup":  # join (or start) a cluster: this server restarts into it
                config = ClusterConfig(svc.data_dir)
                try:
                    folder = str(body.get("folder") or "").strip()
                    if not folder:
                        raise ClusterError("Choose the shared folder on the NAS.")
                    Path(folder).mkdir(parents=True, exist_ok=True)
                    probe = Path(folder) / f".songarr-{svc.port}.probe"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                    config.folder, config.name = folder, clean_name(body.get("name") or "")
                    config.role = body.get("role") if body.get("role") in ROLES else "main"
                    for key in NODE_LOCAL:  # this server's own folders and address, before a shared copy
                        if key not in config.local and (value := svc.db.setting(key)) not in (None, ""):
                            config.local[key] = value  # (which may replace its database) is taken in
                except ClusterError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError as e:
                    return self._json({"error": f"That folder can't be used: {e}"}, 400)
                config.save()
                svc.db.log("cluster", f"Joined the servers sharing {folder} as {config.name} ({config.role}).")
                return self._restart_after({"ok": True, "restarting": True})
            if p == "/api/cluster/leave":
                config = ClusterConfig(svc.data_dir)
                config.folder = ""
                config.save()
                return self._restart_after({"ok": True, "restarting": True})
            if p == "/api/cluster/command":
                if not svc.cluster:
                    return self._json({"error": "This server isn't one of several."}, 400)
                kind = str(body.get("kind") or "")
                if kind not in ("restart", "make-active", "update-check", "settings"):
                    return self._json({"error": "unknown request"}, 400)
                try:
                    result = svc.cluster.command(str(body.get("to") or svc.cluster.name), kind, body.get("values"))
                except (ClusterError, OSError) as e:
                    return self._json({"error": str(e)}, 503)
                return self._json(result, result.get("status") or 200)
            return self._json({"error": "not found"}, 404)

        def _restart_after(self, answer: dict) -> None:
            if svc.restart_hook is None:
                return self._json({"error": "Saved. Stop and start Songarr to use it."}, 200)
            self._json(answer)
            threading.Timer(0.5, svc.restart_hook).start()

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # -- GET ---------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._get()
            finally:
                svc.db.release()  # one thread per request: don't leak its connection

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post()
            finally:
                svc.db.release()

        def _get(self) -> None:
            if not self._host_ok():
                return
            url = urlparse(self.path)
            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            p = url.path
            if svc.standby and svc.cluster and _forwarded(p, post=False):
                return self._forward("GET")
            try:
                if p == "/api/cluster":
                    return self._json(self._cluster_info())
                if p in ("/", "/index.html"):
                    self._send(200, _ui(svc), "text/html; charset=utf-8")
                elif p == "/favicon.ico":
                    self._send(200, svc.branding.ico().read_bytes(), "image/x-icon")
                elif m := re.fullmatch(r"/brand/icon-(\d+)\.png", p):
                    icon = svc.branding.png(int(m[1]))
                    if icon:
                        self._send(200, icon.read_bytes(), "image/png")
                    else:
                        self._json({"error": "no icon chosen"}, 404)
                elif p == "/api/branding":
                    self._json(svc.branding.info() | {"app": svc.app_build.check()})
                elif p == "/api/status":
                    self._json(self._status())
                elif p == "/api/tracks":
                    self._json(self._tracks(qs))
                elif m := re.fullmatch(r"/api/tracks/([A-Za-z0-9]+)/candidates", p):
                    rows = svc.db.q("SELECT * FROM candidates WHERE track_id = ? ORDER BY score DESC", (m[1],))
                    self._json([dict(r) for r in rows])
                elif p == "/api/activity":
                    self._json({"active": svc.snapshot(), "workers": svc.workers(), "paused": svc.paused,
                                "cooldown_until": svc.cooldown_until if svc.cooldown_until > time.time() else None,
                                "pacing": svc.throttle_info()})
                elif p == "/api/playlists":
                    self._json(self._playlists())
                elif p == "/api/history":
                    limit = min(500, int(qs.get("limit", 100)))
                    rows = svc.db.q(
                        """SELECT h.*, t.title, t.artists FROM history h LEFT JOIN tracks t ON t.id = h.track_id
                           ORDER BY h.id DESC LIMIT ?""", (limit,))
                    self._json([dict(r) | {"artists": json.loads(r["artists"] or "[]")} for r in rows])
                elif p == "/api/users":
                    self._json(svc.users.list())
                elif p == "/api/youtube":
                    st = svc.youtube_signin.status()
                    st["in_use"] = svc.db.setting("cookies_file") == str(svc.youtube_signin.cookies_path)
                    if st["window_open"]:
                        try:
                            st["signed_in"] = svc.youtube_signin.signed_in()
                        except Exception:
                            st["signed_in"] = False
                    self._json(st)
                elif p == "/api/settings":
                    s = svc.db.settings()
                    self._json({k: s.get(k) for k in SETTING_RULES})
                else:
                    self._send(404, '{"error":"not found"}')
            except Exception as e:
                log.exception("GET %s failed", p)
                self._json({"error": str(e)}, 500)

        def _status(self) -> dict:
            s = svc.db.settings()
            js = find_js_runtime()
            return {
                "version": __version__,
                "yt_dlp": version("yt-dlp"),
                "brand": svc.branding.info(),
                "cluster": svc.cluster.info() if svc.cluster else None,
                "scan": svc.scanner.info(),
                "dependencies": svc.dependencies.info(),
                "counts": svc.db.counts(),
                "people": {"count": svc.db.one("SELECT COUNT(*) FROM users")[0],
                           "likes": svc.db.one("SELECT COUNT(*) FROM likes")[0]},
                "pacing": svc.throttle_info(),
                "active": len(svc.active),
                "workers": svc.workers(),
                "paused": svc.paused,
                "cooldown_until": svc.cooldown_until if svc.cooldown_until > time.time() else None,
                "checks": {
                    "ffmpeg": find_ffmpeg(s.get("ffmpeg_path") or ""),
                    "js_runtime": next(iter(js), None),
                    "cookies": bool(s["cookies_file"]) and Path(s["cookies_file"]).exists(),
                    "library": Path(s["library_root"]).exists() or Path(s["library_root"]).parent.exists(),
                },
            }

        def _tracks(self, qs: dict) -> dict:
            where, params = ["1=1"], []
            status = qs.get("status", "")
            if status == "missing":
                where.append("t.status IN ('wanted','searching','downloading','failed','review') AND t.monitored = 1")
            elif status == "unmonitored":
                where.append("t.monitored = 0")
            elif status in STATUSES:
                where.append("t.status = ? AND t.monitored = 1")
                params.append(status)
            if pl := qs.get("playlist", ""):  # like:<person> or up:<playlist> (see _playlists)
                where.append("t.id IN (SELECT track_id FROM track_sources WHERE source = ?)")
                params.append(pl)
            if q := qs.get("q", "").strip():
                where.append("(t.title LIKE ? OR t.artists LIKE ? OR t.album LIKE ?)")
                params += [f"%{q}%"] * 3
            order = {"added": "t.added_at DESC", "artist": "t.artists, t.album, t.disc_number, t.track_number",
                     "title": "t.title COLLATE NOCASE"}.get(qs.get("sort", "added"), "t.added_at DESC")
            limit, offset = min(500, int(qs.get("limit", 100))), int(qs.get("offset", 0))
            clause = " AND ".join(where)
            total = svc.db.one(f"SELECT COUNT(*) FROM tracks t WHERE {clause}", params)[0]
            rows = svc.db.q(f"SELECT t.* FROM tracks t WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])
            return {"total": total, "items": [track_dict(r) for r in rows]}

        def _playlists(self) -> list[dict]:
            """Everyone's Liked Songs and playlists, with how much of each is downloaded. The ids filter
            the Library (like:<person>, up:<playlist>: their sources, see track_sources)."""
            counts = {r[0]: (r[1], r[2] or 0) for r in svc.db.q(
                """SELECT s.source, COUNT(*), SUM(t.status = 'downloaded') FROM track_sources s
                   JOIN tracks t ON t.id = s.track_id WHERE s.source GLOB 'like:*' OR s.source GLOB 'up:*'
                   GROUP BY s.source""")}
            people = {r[0]: r[1] for r in svc.db.q("SELECT id, name FROM users ORDER BY id")}
            out = []
            for uid, name in people.items():
                n = counts.get(f"like:{uid}", (0, 0))
                if n[0]:
                    out.append({"id": f"like:{uid}", "name": "Liked Songs", "owner": name, "image_url": None,
                                "songs": n[0], "downloaded": n[1], "liked": True})
            for r in svc.db.q("""SELECT p.id, p.name, p.user_id, p.image_url FROM user_playlists p JOIN users u ON u.id = p.user_id
                                 ORDER BY u.id, p.name COLLATE NOCASE"""):
                n = counts.get(f"up:{r['id']}", (0, 0))
                out.append({"id": f"up:{r['id']}", "name": r["name"], "owner": people.get(r["user_id"]),
                            "image_url": r["image_url"], "songs": n[0], "downloaded": n[1], "liked": False})
            return out

        # -- POST ----------------------------------------------------------------

        def _post(self) -> None:
            if not self._host_ok():
                return
            if self.headers.get("X-Songarr") != "1":
                self._send(403, '{"error":"missing X-Songarr header"}')
                return
            p = urlparse(self.path).path
            if svc.standby and svc.cluster and _forwarded(p, post=True):
                n = int(self.headers.get("Content-Length") or 0)
                return self._forward("POST", self.rfile.read(n) if n else None)
            try:
                body = self._body()
                if p.startswith("/api/cluster"):
                    return self._cluster_post(p, body)
                if p == "/api/settings":
                    folder = svc.db.setting("library_root")
                    for k, v in body.items():
                        if k in SETTING_RULES:
                            svc.db.set_setting(k, SETTING_RULES[k](v))
                    if svc.db.setting("library_root") != folder:
                        svc.scanner.wake.set()  # a new music folder: see what's in it
                    svc.wake.set()
                elif p == "/api/library/scan":
                    svc.scanner.wake.set()
                    return self._json({"ok": True})
                elif p == "/api/users":
                    uid = svc.users.create(str(body.get("name") or ""))
                    svc.db.log("people", f"Added profile {body.get('name')}")
                    return self._json({"ok": True, "id": uid})
                elif m := re.fullmatch(r"/api/users/(\d+)/import", p):  # an Exportify .csv or .zip into a profile
                    if not svc.db.one("SELECT 1 FROM users WHERE id = ?", (int(m[1]),)):
                        return self._json({"error": "There's no such person."}, 404)
                    try:
                        data = base64.b64decode(body.get("data") or "", validate=True)
                        return self._json(exportify.import_upload(svc, int(m[1]), str(body.get("filename") or ""), data))
                    except (binascii.Error, ValueError):
                        return self._json({"error": "That file didn't arrive whole. Try again."}, 400)
                    except exportify.ExportifyError as e:
                        return self._json({"error": str(e)}, 400)
                elif m := re.fullmatch(r"/api/users/(\d+)/(rename|delete|pair|password)", p):
                    uid, action = int(m[1]), m[2]
                    if not svc.db.one("SELECT 1 FROM users WHERE id = ?", (uid,)):
                        return self._json({"error": "unknown profile"}, 404)
                    if action == "rename":
                        svc.users.rename(uid, str(body.get("name") or ""))
                    elif action == "delete":
                        svc.users.delete(uid)
                    elif action == "password":
                        if body.get("clear"):
                            svc.users.clear_password(uid)
                            svc.db.log("people", f"Removed the password for profile {uid}")
                        else:
                            svc.users.set_password(uid, str(body.get("login") or ""), str(body.get("password") or ""))
                            svc.db.log("people", f"Set a password for profile {uid}")
                    else:
                        return self._json(self._pairing(uid))
                elif m := re.fullmatch(r"/api/devices/(\d+)/revoke", p):
                    svc.users.revoke_device(int(m[1]))
                elif p == "/api/youtube/open":
                    svc.youtube_signin.open_window()
                elif p == "/api/youtube/finish":
                    result = svc.youtube_signin.finish()
                    svc.db.set_setting("cookies_file", result["path"])
                    svc.blocks, svc.rate_factor = 0, 1.0
                    svc.resume()
                    result["retried"] = svc.retry_needing_signin()
                    svc.db.log("youtube", "Signed in to YouTube; downloads continue as a signed-in user")
                    return self._json({"ok": True} | result)
                elif p == "/api/youtube/cancel":
                    svc.youtube_signin.close_window()
                elif p == "/api/branding":
                    try:
                        icon = base64.b64decode(body["icon"], validate=True) if body.get("icon") else None
                        info = svc.branding.set(name=body.get("name"), icon=icon, reset_icon=bool(body.get("reset_icon")),
                                                background=body.get("background"))
                    except (BrandError, binascii.Error) as e:
                        return self._json({"error": str(e) if isinstance(e, BrandError) else "That picture didn't upload properly."}, 400)
                    return self._json(info | {"app": svc.app_build.check()})
                elif p == "/api/branding/rebuild-app":
                    try:
                        svc.app_build.start()
                    except ValueError as e:
                        return self._json({"error": str(e)}, 409)
                    return self._json({"app": svc.app_build.check()})
                elif p == "/api/system/update-check":  # "Check now": look for new releases and install them
                    result = svc.dependencies.check(install=True)
                    return self._json(result | {"dependencies": svc.dependencies.info()})
                elif p == "/api/system/restart":
                    if svc.restart_hook is None:
                        return self._json({"error": "Songarr can't restart itself here; stop and start it again."}, 409)
                    threading.Thread(target=svc.restart_hook, daemon=True).start()
                    return self._json({"ok": True})
                elif p == "/api/system/shutdown":
                    svc.db.log("system", "Shut down from the web UI")
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif p == "/api/queue/pause":
                    svc.paused = True
                elif p == "/api/queue/resume":
                    svc.resume()
                elif p == "/api/retry-review":
                    return self._json({"ok": True, "count": svc.retry_review()})
                elif p == "/api/retry-failed":
                    n = svc.db.run("UPDATE tracks SET status='wanted', attempts=0, next_attempt=NULL, error=NULL "
                                   "WHERE status = 'failed' AND monitored = 1")
                    svc.wake.set()
                    return self._json({"ok": True, "count": n})
                elif m := re.fullmatch(r"/api/tracks/([A-Za-z0-9]+)/(retry|ignore|unignore|pick|delete)", p):
                    tid, action = m[1], m[2]
                    if not svc.db.one("SELECT 1 FROM tracks WHERE id = ?", (tid,)):
                        return self._json({"error": "unknown track"}, 404)
                    if tid in svc.active:
                        return self._json({"error": "that song is being processed right now"}, 409)
                    if action == "retry":
                        svc.retry(tid)
                    elif action == "ignore":
                        svc.ignore(tid, True)
                    elif action == "unignore":
                        svc.ignore(tid, False)
                    elif action == "delete":
                        svc.delete_file(tid, download_again=bool(body.get("again")))
                    else:
                        vid = youtube_id(str(body.get("youtube", "")))
                        if not vid:
                            return self._json({"error": "That doesn't look like a YouTube link or video id."}, 400)
                        svc.pick(tid, vid)
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except (ValueError, TypeError) as e:
                self._json({"error": str(e) if isinstance(e, ValueError) and not str(e).startswith("invalid literal") else f"bad value: {e}"}, 400)
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                log.exception("POST %s failed", p)
                self._json({"error": str(e)}, 500)

        def _pairing(self, uid: int) -> dict:
            code, expires = svc.users.new_pairing_code(uid)
            server = svc.db.setting("public_url") or ""
            payload = f"songarr://pair?server={quote(server, safe='')}&code={code}"
            svg = segno.make(payload, error="m").svg_inline(scale=5, border=2, dark="#111111", light="#ffffff")
            return {"code": f"{code[:5]}-{code[5:]}", "expires": expires, "server": server, "qr_svg": svg}

    return ThreadingHTTPServer((host, port), Handler)
