"""python -m songarr [--port 8484] [--data C:\\ProgramData\\Songarr] [--open] [--no-tray]

Setting up (the Windows installer runs these; each does its job and exits):
  --shortcut / --start-menu / --autostart   shortcuts on the desktop, in the Start menu, at sign-in
  --brand-name NAME --brand-icon PICTURE    what it's called and its icon (Settings → Name and icon)
  --music-folder FOLDER                     where music is saved (and already is)
  --stop / --remove-shortcuts               stop a running Songarr; remove its shortcuts
  --cluster-folder F --cluster-name N --cluster-role main|backup [--app-address URL]
                                            one of several servers sharing F on the NAS (cluster.py)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .service import Service
from .appapi import make_app_server
from .web import make_server


RESTART_EXIT = 75  # "start me again" for systemd


def main(argv: list[str] | None = None) -> int:
    default_data = Path(os.environ.get("PROGRAMDATA", Path.home())) / "Songarr"
    ap = argparse.ArgumentParser(prog="songarr", description="Your family's music server: downloads the songs people like, for the Songarr app.")
    ap.add_argument("--data", type=Path, default=default_data, help=f"database, logs and temp files (default {default_data})")
    ap.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8484, help="admin website (keep it private)")
    ap.add_argument("--app-port", type=int, default=8486, help="app API, the only part to expose (e.g. via Cloudflare Tunnel)")
    ap.add_argument("--app-host", default="127.0.0.1", help="interface for the app API (default 127.0.0.1)")
    ap.add_argument("--open", action="store_true", help="open the web UI in your browser")
    ap.add_argument("--no-tray", action="store_true", help="no Songarr icon by the clock (Windows)")
    ap.add_argument("--shortcut", action="store_true", help="put a Songarr shortcut on the desktop, then exit")
    ap.add_argument("--brand-name", metavar="NAME", help="what Songarr is called in the website, apps and shortcuts; then exit")
    ap.add_argument("--brand-icon", metavar="PICTURE", type=Path, help="its icon (PNG, JPEG, WebP, GIF, BMP or ICO); then exit")
    ap.add_argument("--brand-background", metavar="#RRGGBB", help="behind the icon on Android home screens; then exit")
    ap.add_argument("--remove-shortcuts", action="store_true", help="remove Songarr's shortcuts (uninstalling), then exit")
    ap.add_argument("--start-menu", action="store_true", help="put a Songarr shortcut in the Start menu, then exit")
    ap.add_argument("--autostart", action="store_true", help="start Songarr when you sign in to Windows, then exit")
    ap.add_argument("--music-folder", metavar="FOLDER", type=Path, help="where music is saved (and looked for); then exit")
    ap.add_argument("--stop", action="store_true", help="stop the Songarr running on --port, then exit")
    ap.add_argument("--cluster-folder", metavar="FOLDER",
                    help="a folder on the NAS that every Songarr server sharing the music can reach (\"\" to leave); then exit")
    ap.add_argument("--cluster-name", metavar="NAME", help="this server's name among them")
    ap.add_argument("--cluster-role", choices=("main", "backup"),
                    help="main: active whenever it's running; backup: takes over while the main one is away")
    ap.add_argument("--app-address", metavar="URL", help="the address phones reach this server at (https://...); then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.stop:
        return _stop(args.port)
    if (args.brand_name is not None or args.brand_icon or args.brand_background or args.shortcut or args.remove_shortcuts
            or args.start_menu or args.autostart or args.music_folder or args.cluster_folder is not None
            or args.cluster_name or args.cluster_role or args.app_address):
        return _setup(args, argv if argv is not None else sys.argv[1:])

    args.data.mkdir(parents=True, exist_ok=True)
    (args.data / "logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(args.data / "logs" / "songarr.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    if sys.stderr:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("songarr")

    url = f"http://127.0.0.1:{args.port}"
    for host, port in ((args.host, args.port), (args.app_host, args.app_port)):
        with socket.socket() as s:
            if s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0:
                log.info("Songarr is already running (port %d is in use); not starting a second copy", port)
                if args.open:  # the desktop shortcut, clicked while Songarr runs: just show it
                    webbrowser.open(url)
                return 0
    # One of several servers sharing the music (cluster.py): active, or standing by for the active one
    from .cluster import Cluster, ClusterConfig
    config = ClusterConfig(args.data)
    cluster = Cluster(args.data, config) if config.enabled else None
    if cluster:
        try:
            role = cluster.startup()
        except OSError as e:
            log.warning("the cluster folder %s can't be reached (%s); standing by until it can", config.folder, e)
            role = "standby"
        if role == "standby":
            try:
                cluster.import_snapshot()  # the latest copy before anything opens the database
            except OSError as e:
                log.warning("couldn't copy the latest snapshot yet: %s", e)
        log.info("this server (%s) is %s in %s", config.name, role, config.folder)
    svc = Service(args.data, args.port)
    if cluster:
        cluster.attach(svc)
    server = make_server(svc, args.host, args.port)
    app_server = make_app_server(svc, args.app_host, args.app_port)
    threading.Thread(target=app_server.serve_forever, kwargs={"poll_interval": 0.5}, name="app-api", daemon=True).start()
    if not svc.standby:
        svc.start()
    else:  # a standby only keeps its copy (and itself) up to date until it's needed
        svc.start_standby()
    log.info("Songarr %s running at %s (data in %s); app API on http://%s:%d/api/v1",
             __version__, url, args.data, args.app_host, args.app_port)
    if args.open:
        threading.Timer(1.0, webbrowser.open, (url,)).start()

    def shutdown(*_: object) -> None:
        log.info("shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    restarting = threading.Event()

    def restart() -> None:
        """Stop, then start a fresh copy (after an update): it loads the new packages."""
        restarting.set()
        shutdown()

    svc.restart_hook = restart
    stopped = threading.Event()  # everything has stopped (and a standby server has the latest data)

    def session_end() -> None:
        """Windows is shutting down, restarting or signing out: stop as if on purpose, and wait for that
        (Windows ends the program once this returns)."""
        shutdown()
        stopped.wait(30)

    tray = None
    if not args.no_tray:
        from .tray import Tray
        tray = Tray(svc, url, args.data / "logs", stop=shutdown, restart=restart, session_end=session_end)
        if not tray.start():
            tray = None

    def rebranded() -> None:
        """A new name or icon (Settings → Name and icon): the tray icon and the shortcuts follow."""
        if tray:
            tray.brand_changed()
        if sys.platform == "win32":
            threading.Thread(target=_update_shortcuts, args=(svc,), name="shortcuts", daemon=True).start()

    svc.branding.listeners.append(rebranded)

    if cluster:
        threading.Thread(target=cluster.run, name="cluster", daemon=True).start()
        threading.Thread(target=cluster.serve_commands, name="cluster-commands", daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown)
    if sys.platform != "win32":  # systemctl stop: stop properly (a backup server takes over straight away)
        signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if tray:
            tray.stop()
        app_server.shutdown()
        app_server.server_close()
        svc.youtube_signin.close_window()
        svc.stop()
        if cluster and not restarting.is_set():
            cluster.on_exit()  # stopped on purpose: hand over to a standby server, so nothing is lost
        server.server_close()
        if restarting.is_set() and not os.environ.get("INVOCATION_ID"):
            # the ports are closed now, so the new copy can open them; it runs without a window, like this one
            args_again = [a for a in (argv if argv is not None else sys.argv[1:]) if a != "--open"]
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([sys.executable, "-m", "songarr", *args_again], cwd=os.getcwd(), creationflags=flags, close_fds=True)
            log.info("restarted")
        stopped.set()
    if restarting.is_set() and os.environ.get("INVOCATION_ID"):
        # run by systemd (Linux), which would stop a copy started from here along with this one:
        # it starts Songarr again itself on this exit code (RestartForceExitStatus=75 in songarr.service)
        log.info("restarting (systemd starts Songarr again)")
        return RESTART_EXIT
    return 0


def _update_shortcuts(svc: Service) -> None:
    from .shortcut import update_shortcuts
    log = logging.getLogger("songarr")
    try:
        moved = update_shortcuts(svc.branding.name(), svc.branding.ico())
        log.info("shortcuts now: %s", ", ".join(moved) or "none found")
        svc.branding.tidy()  # nothing points at the old icon any more
    except Exception:
        log.exception("couldn't update the shortcuts")
    finally:
        svc.db.release()


def _stop(port: int) -> int:
    """Ask the Songarr on [port] to stop, and wait until it has (before an update or uninstalling)."""
    import time
    import urllib.request

    def running() -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    if not running():
        print("Songarr isn't running.")
        return 0
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown", data=b"{}", method="POST",
                                 headers={"X-Songarr": "1", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:
        print(f"Couldn't ask Songarr to stop: {e}", file=sys.stderr)
        return 1
    for _ in range(60):
        if not running():
            time.sleep(1.5)  # the process ends just after it closes its ports
            print("Stopped Songarr.")
            return 0
        time.sleep(0.5)
    print("Songarr didn't stop in time.", file=sys.stderr)
    return 1


def _setup(args: argparse.Namespace, given: list[str]) -> int:
    """--brand-name/--brand-icon, --music-folder, the shortcuts and --remove-shortcuts: set up, then exit
    (the installer runs these)."""
    from .branding import Branding, BrandError
    from .cluster import NODE_LOCAL, ClusterConfig, ClusterError, clean_name
    from .db import DB
    from .shortcut import make_shortcut, remove_shortcuts

    if args.remove_shortcuts:
        for path in remove_shortcuts():
            print(f"Removed {path}")
        return 0
    args.data.mkdir(parents=True, exist_ok=True)
    config = ClusterConfig(args.data)
    if args.cluster_folder is not None or args.cluster_name or args.cluster_role:
        try:
            if args.cluster_folder is not None:
                config.folder = args.cluster_folder.strip()
                if config.folder:
                    Path(config.folder).mkdir(parents=True, exist_ok=True)
            if args.cluster_name or (config.folder and not config.name):
                config.name = clean_name(args.cluster_name or socket.gethostname())
            if args.cluster_role:
                config.role = args.cluster_role
        except (ClusterError, OSError) as e:
            print(f"Couldn't set up the cluster: {e}", file=sys.stderr)
            return 2
        config.save()
        print(f"Cluster: {config.folder or '(none)'}; this server: {config.name} ({config.role})")
    db = DB(args.data / "songarr.db")
    if config.enabled:  # this server's own settings go to cluster.json
        for key in NODE_LOCAL:
            if key not in config.local and (value := db.setting(key)) not in (None, ""):
                config.local[key] = value
        db.local, db.local_keys = dict(config.local), NODE_LOCAL

        def save_local(values: dict) -> None:
            config.local = dict(values)
            config.save()
        db.save_local = save_local
    try:
        if args.app_address:
            if not args.app_address.startswith(("https://", "http://")):
                print("The app address starts with https:// (or http:// at home).", file=sys.stderr)
                return 2
            db.set_setting("public_url", args.app_address.rstrip("/"))
            print(f"App address: {args.app_address.rstrip('/')}")
        branding = Branding(db, args.data)
        if args.brand_name is not None or args.brand_icon or args.brand_background:
            try:
                icon = args.brand_icon.read_bytes() if args.brand_icon else None
                info = branding.set(name=args.brand_name, icon=icon, background=args.brand_background)
            except (BrandError, OSError) as e:
                print(f"Couldn't set the name and icon: {e}", file=sys.stderr)
                return 2
            print(f"Name: {info['name']}; icon: {'chosen picture' if info['icon'] else 'Songarr'}")
        if args.music_folder:
            try:
                args.music_folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"Couldn't make the music folder: {e}", file=sys.stderr)
                return 2
            db.set_setting("library_root", str(args.music_folder))
            print(f"Music folder: {args.music_folder}")
        wanted = [w for w, on in (("desktop", args.shortcut), ("start-menu", args.start_menu), ("autostart", args.autostart)) if on]
        if wanted:
            setup_only = {"--shortcut", "--open", "--remove-shortcuts", "--start-menu", "--autostart"}
            with_values = {"--brand-name", "--brand-icon", "--brand-background", "--music-folder", "--cluster-folder",
                           "--cluster-name", "--cluster-role", "--app-address"}
            keep, skip = [], False
            for a in given:  # what's left (like --data or --port) goes into the shortcuts
                if skip:
                    skip = False
                elif a in with_values:
                    skip = True
                elif a not in setup_only and a.split("=")[0] not in with_values:
                    keep.append(a)
            for where in wanted:
                print(f"Made {make_shortcut(where, keep, branding.name(), branding.ico())}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
