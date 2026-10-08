"""New versions of the Android app, handed to paired phones by the app API.

Publish a build after `flutter build apk --release --split-per-abi`:

    .venv\\Scripts\\python.exe -m songarr.app_updates publish --notes "What changed"

The newest APK for each processor type (arm64-v8a, armeabi-v7a, x86_64) is copied into
<data>/app-updates with its size and SHA-256, and the apps offer it on their next check.
Android only installs an update signed with the same key as the installed app.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")
PROGRAM_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA = Path(os.environ.get("PROGRAMDATA", Path.home())) / "Songarr"


class AppUpdates:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "app-updates"
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def manifest(self) -> dict:
        path = self.dir / "manifest.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {"files": {}}
        with self._lock:
            if self._cache and self._cache[0] == mtime:
                return self._cache[1]
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"files": {}}
            self._cache = (mtime, data)
            return data

    def latest(self, abi: str) -> dict | None:
        entry = self.manifest().get("files", {}).get(abi)
        if not entry or not (self.dir / entry["file"]).is_file():
            return None
        return {k: entry[k] for k in ("version", "build", "notes", "published", "size", "sha256")} | {"abi": abi}

    def apk(self, abi: str) -> Path | None:
        entry = self.manifest().get("files", {}).get(abi)
        path = self.dir / entry["file"] if entry else None
        return path if path and path.is_file() else None


def pubspec_version(app_dir: Path) -> tuple[str, int]:
    text = (app_dir / "pubspec.yaml").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*([0-9][0-9.]*)\+(\d+)\s*$", text, re.M)
    if not m:
        raise SystemExit("pubspec.yaml has no 'version: x.y.z+build' line")
    return m[1], int(m[2])


def publish(data_dir: Path, app_dir: Path, notes: str = "", abis: tuple[str, ...] = ABIS,
            version: str | None = None, build: int | None = None) -> dict:
    """Copy freshly built APKs into the update folder. Returns the new manifest. [version] and [build]:
    what they were built as (`flutter build --build-name --build-number`), if not pubspec.yaml's."""
    if version is None or build is None:
        version, build = pubspec_version(app_dir)
    out = Path(data_dir) / "app-updates"
    out.mkdir(parents=True, exist_ok=True)
    built = app_dir / "build" / "app" / "outputs" / "flutter-apk"
    sources = {abi: built / f"app-{abi}-release.apk" for abi in abis}
    present = {abi: p for abi, p in sources.items() if p.is_file()}
    if not present:
        raise SystemExit(f"No release APKs in {built}; build them first.")
    newest = max(p.stat().st_mtime for p in present.values())
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"files": {}}
    for abi, src in present.items():
        if newest - src.stat().st_mtime > 3600:  # left over from an older build: don't relabel it
            print(f"skipping {abi}: {src.name} is older than this build", file=sys.stderr)
            continue
        name = f"songarr-{version}+{build}-{abi}.apk"
        tmp = out / (name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, out / name)
        digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
        manifest["files"][abi] = {"file": name, "version": version, "build": build, "notes": notes.strip(),
                                  "published": time.time(), "size": (out / name).stat().st_size, "sha256": digest}
    tmp = out / "manifest.json.part"
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)
    keep = {e["file"] for e in manifest["files"].values()} | {"manifest.json"}
    for f in out.iterdir():
        if f.name not in keep:
            f.unlink(missing_ok=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m songarr.app_updates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="offer the APKs just built to the apps")
    p.add_argument("--notes", default="", help="what's new, shown in the app")
    p.add_argument("--abi", action="append", choices=ABIS, help="only these processor types (default: all built)")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--app", type=Path, default=PROGRAM_DIR / "app")
    args = ap.parse_args(argv)
    _, build = pubspec_version(args.app)
    offered = AppUpdates(args.data).manifest().get("files", {})
    newest = max((e.get("build", 0) for abi, e in offered.items() if abi in (args.abi or ABIS)), default=0)
    if build <= newest:  # (an app rebuilt on the server for a new name or icon counts on from there)
        print(f"Note: phones were already offered build {newest}, so they won't install build {build}. To offer "
              f"this one, raise the number after '+' in app/pubspec.yaml past {newest}, build again and publish.",
              file=sys.stderr)
    manifest = publish(args.data, args.app, args.notes, tuple(args.abi or ABIS))
    for abi, e in sorted(manifest["files"].items()):
        print(f"{abi:12} {e['version']}+{e['build']}  {e['size'] / 1e6:.1f} MB  {e['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
