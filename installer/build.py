"""Builds the Windows installer, dist/Songarr-Setup-<version>.exe.

    .venv\\Scripts\\python.exe installer\\build.py

Run it from the project's virtual environment (the packages installed there go into the
installer). Needs Inno Setup 6 (winget install JRSoftware.InnoSetup).

The installer carries its own Python (copied from the one this environment was made from, minus
its tests and Tk) with Songarr's packages, so nobody has to install Python. Songarr's automatic
updates keep yt-dlp current inside it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "installer"
APP = STAGE / "app"
DIST = ROOT / "dist"

# not needed by a server: tests, Tk, the IDE, the bundled pip wheel, and anything compiled
SKIP_LIB = {"test", "tkinter", "idlelib", "turtledemo", "ensurepip", "site-packages", "__pycache__", "lib2to3"}
SKIP_DLLS = {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "_testcapi.pyd", "_testinternalcapi.pyd", "_testbuffer.pyd",
             "_testimportmultiple.pyd", "_testmultiphase.pyd", "_testsinglephase.pyd", "_testclinic.pyd",
             "_testlimitedcapi.pyd", "_testconsole.pyd", "_ctypes_test.pyd", "xxlimited.pyd", "xxlimited_35.pyd"}
CHECK = "import songarr.__main__, songarr.tray, yt_dlp, PIL, mutagen, ytmusicapi, websockets, segno, pip; print('ok')"


def ignore(names: set[str]):
    return lambda folder, entries: [e for e in entries if e in names or e.endswith((".pyc", ".pyo"))]


def find_iscc() -> Path:
    for candidate in (os.environ.get("ISCC"), Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
                      Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"), Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit("Inno Setup 6 isn't installed: winget install JRSoftware.InnoSetup")


def runtime() -> Path:
    """A private copy of this Python with Songarr's packages: APP/runtime."""
    base = Path(sys.base_prefix)
    if base == Path(sys.prefix):
        raise SystemExit("Run this from Songarr's virtual environment (.venv), so its packages go in.")
    rt = APP / "runtime"
    rt.mkdir(parents=True)
    for f in base.iterdir():
        if f.is_file() and f.suffix.lower() in (".exe", ".dll") or f.name == "LICENSE.txt":
            shutil.copy2(f, rt / f.name)
    shutil.copytree(base / "DLLs", rt / "DLLs", ignore=ignore(SKIP_DLLS))
    shutil.copytree(base / "Lib", rt / "Lib", ignore=ignore(SKIP_LIB))
    site = rt / "Lib" / "site-packages"
    shutil.copytree(sysconfig.get_paths()["purelib"], site, ignore=ignore({"__pycache__", "_virtualenv.py", "_virtualenv.pth"}))
    # Songarr's own folder (the one with the songarr package) is on the path wherever it's started from
    (site / "songarr-app.pth").write_text("../../..\n", encoding="utf-8")
    return rt


def artwork() -> None:
    """The wizard's pictures, from Songarr's icon, at the sizes Windows' display scaling asks for."""
    from PIL import Image, ImageDraw

    icon = Image.open(ROOT / "songarr" / "assets" / "songarr.ico")
    icon.size = max(icon.ico.sizes())
    note = icon.convert("RGBA")
    out = STAGE / "art"
    out.mkdir()
    for w, h in ((164, 314), (205, 392), (246, 459), (328, 628)):  # 100% to 200%
        img = Image.new("RGB", (w, h))
        px = ImageDraw.Draw(img)
        for y in range(h):  # Songarr's night sky: violet into near black
            t = y / h
            px.line([(0, y), (w, y)], fill=(int(46 - 28 * t), int(24 - 6 * t), int(74 - 46 * t)))
        size = int(w * 0.62)
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((w / 2 - size * 0.7, h * 0.38 - size * 0.7, w / 2 + size * 0.7, h * 0.38 + size * 0.7),
                                     fill=(139, 92, 246, 40))
        img = Image.alpha_composite(img.convert("RGBA"), glow)
        mark = note.resize((size, size), Image.LANCZOS)
        img.alpha_composite(mark, ((w - size) // 2, int(h * 0.38) - size // 2))
        img.convert("RGB").save(out / f"wizard-{w}.png")
    for s in (55, 69, 83, 110):  # see-through, for light and dark mode alike
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        mark = note.resize((int(s * 0.86), int(s * 0.86)), Image.LANCZOS)
        img.alpha_composite(mark, ((s - mark.width) // 2, (s - mark.height) // 2))
        img.save(out / f"small-{s}.png")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from songarr import __version__

    shutil.rmtree(STAGE, ignore_errors=True)
    APP.mkdir(parents=True)
    shutil.copytree(ROOT / "songarr", APP / "songarr", ignore=ignore({"__pycache__"}))
    for doc in ("README.md", "requirements.txt"):
        shutil.copy2(ROOT / doc, APP / doc)
    rt = runtime()
    python = rt / "python.exe"
    # compile everything once, so the first start is quick (the install folder may not be writable later);
    # -s: the files record where they are inside Songarr's folder, not where this build happened
    subprocess.run([python, "-m", "compileall", "-q", "-j0", "-s", str(APP), str(rt / "Lib"), str(APP / "songarr")],
                   check=True, stdout=subprocess.DEVNULL)
    check = subprocess.run([python, "-I", "-c", CHECK], cwd=STAGE, capture_output=True, text=True)  # -I: nothing from here
    if check.stdout.strip() != "ok":
        raise SystemExit(f"The bundled Python doesn't work:\n{check.stdout}{check.stderr}")
    artwork()
    DIST.mkdir(exist_ok=True)
    iscc = find_iscc()
    subprocess.run([str(iscc), "/Qp", f"/DAppVersion={__version__}", f"/DStage={STAGE}", f"/O{DIST}",
                    str(ROOT / "installer" / "songarr.iss")], check=True)
    setup = DIST / f"Songarr-Setup-{__version__}.exe"
    print(f"Built {setup} ({setup.stat().st_size / 1048576:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
