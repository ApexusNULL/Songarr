"""Songarr's icon in the Windows notification area (by the clock, usually under "Show hidden icons").

Clicking it opens a menu: open the admin website, pause or resume downloads, check for updates,
restart, open the music or log folder, and stop Songarr. Hovering shows what Songarr is doing.
It uses the name and icon chosen in Settings → Name and icon, and changes with them.

Plain ctypes, no extra packages. On other systems, or when there's no desktop to show it on (a
service, a remote session that ended), Songarr simply runs without it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

ICON = Path(__file__).with_name("assets") / "songarr.ico"  # Songarr's own (branding.py may choose another)
REFRESH_MS = 15_000  # how often the hover text is brought up to date


@dataclass
class Item:
    cmd: str
    label: str
    enabled: bool = True
    default: bool = False  # shown bold: what a click on the icon would usually do


SEPARATOR = None


def status_line(svc: Service) -> str:
    """What Songarr is doing, in a few words (the menu's first line and the hover text)."""
    if getattr(svc, "standby", False):  # a backup server while another is active (cluster.py)
        try:
            active = svc.cluster.info().get("active") if svc.cluster else None
        except OSError:
            active = None
        return f"Standing by · {active} is active" if active else "Standing by"
    if svc.draining:
        return "Finishing downloads, then restarting"
    if svc.paused:
        return "Downloads paused"
    if time.time() < svc.cooldown_until:
        return "YouTube break until " + time.strftime("%H:%M", time.localtime(svc.cooldown_until))
    counts = svc.db.counts()
    waiting = counts["wanted"] + counts["searching"]
    active = len(svc.active)
    if active:
        return f"Downloading {active} · {waiting:,} waiting"
    if waiting:
        return f"{waiting:,} song{'s' if waiting != 1 else ''} waiting"
    return f"Up to date · {counts['downloaded']:,} songs"


class Tray:
    def __init__(self, svc: Service, url: str, log_dir: Path, stop: Callable[[], None], restart: Callable[[], None],
                 session_end: Callable[[], None] | None = None):
        self.svc = svc
        self.url = url
        self.log_dir = log_dir
        self.stop_songarr = stop
        self.restart_songarr = restart
        self.session_end = session_end
        self.checking = False
        self.asking = False  # a "Stop Songarr?" question is open
        self._win: _Win | None = None
        # swapped out in tests
        self.open_url: Callable[[str], object] = webbrowser.open
        self.open_folder: Callable[[str], object] = getattr(os, "startfile", lambda p: None)
        self.ask: Callable[[str, str], bool] = lambda title, text: True
        self.notify: Callable[[str, str], None] = lambda title, text: None

    # -- what the menu shows and does (the same on every system, so it can be tested) --------------

    def items(self) -> list[Item | None]:
        deps = self.svc.dependencies.state
        updating = self.checking or deps["updating"]
        library = Path(self.svc.db.setting("library_root") or "")
        name = self.svc.branding.name()
        return [
            Item("open", f"Open {name}", default=True),
            SEPARATOR,
            Item("status", status_line(self.svc), enabled=False),
            *([] if getattr(self.svc, "standby", False) else
              [Item("resume", "Resume downloads") if self.svc.paused else Item("pause", "Pause downloads")]),
            Item("update", "Checking for updates…" if updating else "Check for updates", enabled=not updating),
            Item("restart", "Restart to finish updating" if deps["restart_pending"] else f"Restart {name}"),
            SEPARATOR,
            Item("library", "Open music folder", enabled=library.is_dir()),
            Item("logs", "Open log folder"),
            SEPARATOR,
            Item("stop", f"Stop {name}"),
        ]

    def act(self, cmd: str) -> None:
        """Do what a menu item says. Runs on its own thread: some of these take a while."""
        svc = self.svc
        name = svc.branding.name()
        try:
            if cmd == "open":
                self.open_url(self.url)
            elif cmd == "pause":
                svc.paused = True
            elif cmd == "resume":
                svc.resume()
            elif cmd == "update":
                self.check_for_updates()
            elif cmd == "restart":
                if svc.dependencies.quiet() or self.ask(
                        f"Restart {name}?", f"Someone is listening or in a Jam right now. Their music stops for a few seconds while {name} restarts."):
                    svc.db.log("system", "Restarted from the tray icon")
                    self.restart_songarr()
            elif cmd == "library":
                self.open_folder(str(svc.db.setting("library_root")))
            elif cmd == "logs":
                self.open_folder(str(self.log_dir))
            elif cmd == "stop":
                if self.asking:
                    return
                self.asking = True
                try:
                    if not self.ask(f"Stop {name}?", f"Phones can't play, download or sync until {name} is started again "
                                                     f"(the {name} shortcut on the desktop starts it)."):
                        return
                finally:
                    self.asking = False
                svc.db.log("system", "Stopped from the tray icon")
                self.stop_songarr()
        except Exception:
            log.exception("tray: %s failed", cmd)
        finally:
            svc.db.release()

    def check_for_updates(self) -> None:
        if self.checking:
            return
        self.checking = True
        try:
            result = self.svc.dependencies.check(install=True)
        finally:
            self.checking = False
        found = result.get("found") or {}
        if result.get("error") and not found:
            self.notify("Couldn't check for updates", result["error"])
        elif found:
            what = ", ".join(f"{k} {v['to']}" for k, v in found.items())
            if self.svc.dependencies.state["restart_pending"]:
                self.notify("Updated " + what, f"{self.svc.branding.name()} restarts to use it as soon as nobody's "
                                               "listening, or choose Restart to finish updating.")
            else:
                self.notify("Update didn't work", "The versions that work were kept. See History for details.")
        else:
            versions = self.svc.dependencies.installed_versions()
            self.notify("Everything is up to date", f"yt-dlp {versions.get('yt-dlp', '?')}")

    def session_ending(self) -> None:
        """Windows is shutting down, restarting (an update, say) or signing out: stop properly first, so a
        backup server takes over with everything."""
        if self.session_end:
            self.session_end()

    def tip(self) -> str:
        try:
            return f"{self.svc.branding.name()} · {status_line(self.svc)}"[:127]
        except Exception:  # the database is busy or closing: the name will do
            return "Songarr"
        finally:
            self.svc.db.release()

    # -- the Windows side -------------------------------------------------------------------------

    def start(self) -> bool:
        """Show the icon. False where there's no notification area to put it in."""
        if sys.platform != "win32" or not self.svc.branding.ico().exists():
            return False
        _app_id(self.svc.branding.name())
        self._win = _Win(self)
        self.ask = self._win.ask
        self.notify = self._win.notify
        return self._win.start()

    def stop(self) -> None:
        if self._win:
            self._win.close()
            self._win = None

    def brand_changed(self) -> None:
        """A new name or icon (Settings → Name and icon): show it."""
        if self._win:
            self._win.rebrand()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as w

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD),
                    ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
                    ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD), ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    def _app_id(name: str) -> None:
        """Notifications are labelled with this (rather than "Python")."""
        try:
            _shell32.SetCurrentProcessExplicitAppUserModelID(name)
        except (AttributeError, OSError):
            pass

    def _sig(dll, name, res, *args):
        f = getattr(dll, name)
        f.restype, f.argtypes = res, list(args)
        return f

    DefWindowProcW = _sig(_user32, "DefWindowProcW", LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    RegisterClassW = _sig(_user32, "RegisterClassW", w.ATOM, ctypes.POINTER(WNDCLASSW))
    CreateWindowExW = _sig(_user32, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    DestroyWindow = _sig(_user32, "DestroyWindow", w.BOOL, w.HWND)
    PostMessageW = _sig(_user32, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    PostQuitMessage = _sig(_user32, "PostQuitMessage", None, ctypes.c_int)
    GetMessageW = _sig(_user32, "GetMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
    TranslateMessage = _sig(_user32, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
    DispatchMessageW = _sig(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(w.MSG))
    RegisterWindowMessageW = _sig(_user32, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
    SetTimer = _sig(_user32, "SetTimer", ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p)
    LoadImageW = _sig(_user32, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
    DestroyIcon = _sig(_user32, "DestroyIcon", w.BOOL, w.HICON)
    GetSystemMetrics = _sig(_user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
    CreatePopupMenu = _sig(_user32, "CreatePopupMenu", w.HMENU)
    AppendMenuW = _sig(_user32, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    SetMenuDefaultItem = _sig(_user32, "SetMenuDefaultItem", w.BOOL, w.HMENU, w.UINT, w.UINT)
    TrackPopupMenu = _sig(_user32, "TrackPopupMenu", ctypes.c_int, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, w.HWND, ctypes.c_void_p)
    DestroyMenu = _sig(_user32, "DestroyMenu", w.BOOL, w.HMENU)
    SetForegroundWindow = _sig(_user32, "SetForegroundWindow", w.BOOL, w.HWND)
    GetCursorPos = _sig(_user32, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
    MessageBoxW = _sig(_user32, "MessageBoxW", ctypes.c_int, w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
    Shell_NotifyIconW = _sig(_shell32, "Shell_NotifyIconW", w.BOOL, w.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    GetModuleHandleW = _sig(_kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
    ShutdownBlockReasonCreate = _sig(_user32, "ShutdownBlockReasonCreate", w.BOOL, w.HWND, w.LPCWSTR)
    ShutdownBlockReasonDestroy = _sig(_user32, "ShutdownBlockReasonDestroy", w.BOOL, w.HWND)

    WM_CLOSE, WM_DESTROY, WM_TIMER, WM_NULL, WM_CONTEXTMENU = 0x0010, 0x0002, 0x0113, 0x0000, 0x007B
    WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
    WM_TRAY, WM_NOTE, WM_BRAND = 0x8001, 0x8002, 0x8003  # WM_APP + n: clicks, a notification, a new name or icon
    NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
    NIIF_USER, NIIF_LARGE_ICON = 0x04, 0x20
    MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
    TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
    MB_YESNO, MB_ICONQUESTION, MB_DEFBUTTON2, MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x4, 0x20, 0x100, 0x10000, 0x40000, 6
    IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON, SM_CXICON = 1, 0x10, 49, 11
    CLASS = "SongarrTray"

    class _Win:
        """The hidden window that owns the icon, on a thread of its own with a Windows message loop."""

        def __init__(self, tray: Tray):
            self.tray = tray
            self.hwnd = None
            self.ready = threading.Event()
            self.ok = False
            self.notes: list[tuple[str, str]] = []
            self.lock = threading.Lock()
            self.taskbar_created = self.icon = self.big_icon = None
            self.proc = WNDPROC(self._wndproc)  # kept: Windows calls it for as long as the window lives

        def start(self) -> bool:
            self.thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self.thread.start()
            self.ready.wait(10)
            return self.ok

        def close(self) -> None:
            if self.hwnd:
                PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
                self.thread.join(5)

        def rebrand(self) -> None:
            if self.hwnd:
                PostMessageW(self.hwnd, WM_BRAND, 0, 0)

        def notify(self, title: str, text: str) -> None:
            with self.lock:
                self.notes.append((title, text))
            if self.hwnd:
                PostMessageW(self.hwnd, WM_NOTE, 0, 0)

        def ask(self, title: str, text: str) -> bool:
            flags = MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST
            return MessageBoxW(None, text, title, flags) == IDYES

        # runs on the tray thread from here on

        def _run(self) -> None:
            try:
                # sharp icon and menu on high-DPI screens (pythonw is otherwise scaled up, blurry)
                _user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
                _user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
            except (AttributeError, OSError):
                pass
            hinst = GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self.proc, hInstance=hinst, lpszClassName=CLASS)
            RegisterClassW(ctypes.byref(wc))  # fails harmlessly if it's already registered
            self.hwnd = CreateWindowExW(0, CLASS, "Songarr", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                log.warning("tray: couldn't create its window (error %d)", ctypes.get_last_error())
                self.ready.set()
                return
            self.taskbar_created = RegisterWindowMessageW("TaskbarCreated")  # Explorer restarted: add the icon again
            self._load_icons()
            self.ok = self._add()
            if not self.ok:
                log.info("tray: no notification area to show the icon in (error %d)", ctypes.get_last_error())
            SetTimer(self.hwnd, 1, REFRESH_MS, None)
            self.ready.set()
            msg = w.MSG()
            while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                TranslateMessage(ctypes.byref(msg))
                DispatchMessageW(ctypes.byref(msg))
            self._free_icons()

        def _load_icons(self) -> None:
            path = str(self.tray.svc.branding.ico())
            small, big = self._metric(SM_CXSMICON), self._metric(SM_CXICON)
            self.icon = LoadImageW(None, path, IMAGE_ICON, small, small, LR_LOADFROMFILE)
            self.big_icon = LoadImageW(None, path, IMAGE_ICON, big, big, LR_LOADFROMFILE)

        def _free_icons(self) -> None:
            for icon in (self.icon, self.big_icon):
                if icon:
                    DestroyIcon(icon)

        def _rebrand(self) -> None:
            old = (self.icon, self.big_icon)
            try:
                self._load_icons()
                nid = self._data(NIF_ICON | NIF_TIP | NIF_SHOWTIP)
                nid.hIcon, nid.szTip = self.icon, self.tray.tip()
                Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                _app_id(self.tray.svc.branding.name())
            finally:
                self.tray.svc.db.release()
            for icon in old:
                if icon:
                    DestroyIcon(icon)

        @staticmethod
        def _metric(index: int) -> int:
            try:
                return _user32.GetSystemMetricsForDpi(index, _user32.GetDpiForSystem())
            except (AttributeError, OSError):
                return GetSystemMetrics(index)

        def _data(self, flags: int) -> NOTIFYICONDATAW:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
            return nid

        def _add(self) -> bool:
            nid = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
            nid.uCallbackMessage, nid.hIcon, nid.szTip = WM_TRAY, self.icon, self.tray.tip()
            if not Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                return False
            nid.uVersion = 4  # NOTIFYICON_VERSION_4: clicks and the keyboard arrive as NIN_SELECT / WM_CONTEXTMENU
            Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
            return True

        def _refresh(self) -> None:
            nid = self._data(NIF_TIP | NIF_SHOWTIP)
            nid.szTip = self.tray.tip()
            Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

        def _show_notes(self) -> None:
            with self.lock:
                notes, self.notes = self.notes, []
            for title, text in notes:
                nid = self._data(NIF_INFO)
                nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
                nid.dwInfoFlags, nid.hBalloonIcon = NIIF_USER | NIIF_LARGE_ICON, self.big_icon
                Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

        def _menu(self) -> None:
            try:
                items = self.tray.items()
            finally:
                self.tray.svc.db.release()
            menu = CreatePopupMenu()
            cmds: dict[int, str] = {}
            for n, item in enumerate(items, start=1):
                if item is None:
                    AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                cmds[n] = item.cmd
                AppendMenuW(menu, MF_STRING | (0 if item.enabled else MF_GRAYED), n, item.label)
                if item.default:
                    SetMenuDefaultItem(menu, n, 0)
            pt = w.POINT()
            GetCursorPos(ctypes.byref(pt))
            SetForegroundWindow(self.hwnd)  # otherwise the menu doesn't close when you click elsewhere
            chosen = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
            PostMessageW(self.hwnd, WM_NULL, 0, 0)
            DestroyMenu(menu)
            if chosen in cmds:
                threading.Thread(target=self.tray.act, args=(cmds[chosen],), name="tray-action", daemon=True).start()

        def _wndproc(self, hwnd, msg, wparam, lparam):
            try:
                if msg == WM_TRAY:
                    if lparam & 0xFFFF in (WM_CONTEXTMENU, NIN_SELECT, NIN_KEYSELECT):
                        self._menu()
                    return 0
                if msg == WM_NOTE:
                    self._show_notes()
                    return 0
                if msg == WM_BRAND:
                    self._rebrand()
                    return 0
                if msg == WM_TIMER:
                    self._refresh()
                    return 0
                if msg == self.taskbar_created:
                    self._add()
                    return 0
                if msg == WM_QUERYENDSESSION:
                    return 1  # Windows is shutting down or signing out: fine (WM_ENDSESSION follows)
                if msg == WM_ENDSESSION:
                    if wparam:  # it really is: stop properly before Windows ends the program
                        ShutdownBlockReasonCreate(hwnd, f"Stopping {self.tray.svc.branding.name()} properly…")
                        try:
                            self.tray.session_ending()
                        finally:
                            ShutdownBlockReasonDestroy(hwnd)
                    return 0
                if msg == WM_CLOSE:
                    Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
                    DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    PostQuitMessage(0)
                    return 0
            except Exception:
                log.exception("tray")
            return DefWindowProcW(hwnd, msg, wparam, lparam)
