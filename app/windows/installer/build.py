"""Build the Songarr desktop app for Windows, and its installer (app/build/windows/installer/Songarr-Setup.exe).

    .venv\\Scripts\\python.exe app\\windows\\installer\\build.py

It needs Flutter, Visual Studio's C++ build tools, Windows' Developer Mode (Flutter's plugins use
symbolic links) and Inno Setup 6. The version is pubspec.yaml's; the app is told it, so it can ask
your server for a newer one. Then offer it from the server, like the phone app:

    .venv\\Scripts\\python.exe -m songarr.app_updates publish --abi windows-x64 --notes "What's new"

The desktop app on everyone's PC then updates itself, and your download page offers it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP.parent))
from songarr.app_build import find_flutter  # noqa: E402


def version() -> tuple[str, int]:
    m = re.search(r"^version:\s*([0-9][0-9.]*)\+(\d+)\s*$", (APP / "pubspec.yaml").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("pubspec.yaml has no 'version: x.y.z+build' line")
    return m[1], int(m[2])


def iscc() -> str:
    for candidate in (os.environ.get("ISCC"), Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
                      Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"), Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise SystemExit("Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup), or set ISCC to its ISCC.exe.")


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("The Windows desktop app is built on Windows.")
    flutter = find_flutter()
    if not flutter:
        raise SystemExit("Flutter isn't installed (or isn't on the PATH).")
    ver, build = version()
    defines = Path(tempfile.mkdtemp(prefix="songarr-desktop-")) / "app.json"
    defines.write_text(json.dumps({"APP_VERSION": ver, "APP_BUILD": build}), encoding="utf-8")
    # Flutter can keep the icon font it trimmed for an earlier build (icons used since then show
    # blank): the app's assets are made afresh each time.
    shutil.rmtree(APP / "build" / "flutter_assets", ignore_errors=True)
    try:
        print(f"Building Songarr {ver} ({build}) for Windows…", flush=True)
        subprocess.run([flutter, "build", "windows", "--release", f"--dart-define-from-file={defines}"], cwd=APP, check=True)
    finally:
        shutil.rmtree(defines.parent, ignore_errors=True)
    release = APP / "build" / "windows" / "x64" / "runner" / "Release"
    if not (release / "Songarr.exe").is_file():
        raise SystemExit(f"The build didn't leave Songarr.exe in {release}.")
    out = APP / "build" / "windows" / "installer"
    out.mkdir(parents=True, exist_ok=True)
    print("Making the installer…", flush=True)
    subprocess.run([iscc(), "/Q", f"/DAppVersion={ver}", f"/DSource={release}", f"/O{out}",
                    str(Path(__file__).with_name("songarr-desktop.iss"))], check=True)
    setup = out / "Songarr-Setup.exe"
    print(f"{setup} ({setup.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
