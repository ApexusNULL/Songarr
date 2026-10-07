"""Playlist files: <library>/Playlists/<name>.m3u8 for each person's Liked Songs and playlists.

Entries point at the downloaded files with paths relative to the playlist (portable across
drive letters and to Plex/Jellyfin on the NAS); songs not downloaded yet are left out and
appear as soon as they are. Names carry the person's name only where needed: "Liked Songs" when
one person likes songs (else "Liked Songs (Name)"), and a playlist's own name unless two people
have playlists called the same ("Road Trip (Name)").
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .db import DB
from .library import safe

log = logging.getLogger(__name__)

LIKED_NAME = "Liked Songs"
WRITTEN = "m3u_files"  # setting: the file names Songarr wrote, so ones no longer wanted can be removed


def wanted(db: DB) -> list[tuple[str, str, list[str]]]:
    """(key, playlist name, song ids in order) for every file there should be."""
    people = {r[0]: r[1] for r in db.q("SELECT id, name FROM users ORDER BY id")}
    likers = [uid for (uid,) in db.q("SELECT DISTINCT user_id FROM likes ORDER BY user_id") if uid in people]
    out = [(f"like:{uid}", LIKED_NAME if len(likers) == 1 else f"{LIKED_NAME} ({people[uid]})", db.liked_list(uid))
           for uid in likers]
    lists = db.q("SELECT id, user_id, name FROM user_playlists ORDER BY name COLLATE NOCASE, user_id")
    owners: dict[str, set[int]] = {}
    for r in lists:
        owners.setdefault(r["name"].lower(), set()).add(r["user_id"])
    for r in lists:
        base = r["name"]
        if base.lower() == LIKED_NAME.lower():  # a playlist called "Liked Songs": not to be taken for someone's likes
            base = f"{base} playlist"
        name = base if len(owners[r["name"].lower()]) == 1 else f"{base} ({people.get(r['user_id'], '?')})"
        ids = [t for (t,) in db.q("SELECT track_id FROM user_playlist_tracks WHERE playlist_id = ? ORDER BY position", (r["id"],))]
        out.append((f"up:{r['id']}", name, ids))
    return out


def _entries(db: DB, ids: list[str], folder: Path) -> list[str]:
    rows = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for r in db.q(f"""SELECT id, title, artists, duration_ms, file_path FROM tracks
                          WHERE id IN ({','.join('?' * len(chunk))}) AND status = 'downloaded' AND file_path IS NOT NULL""",
                      chunk):
            rows[r["id"]] = r
    lines = []
    for tid in ids:
        if (r := rows.get(tid)) is None:
            continue
        try:
            path = os.path.relpath(r["file_path"], folder).replace("\\", "/")
        except ValueError:  # different drive: fall back to the absolute path
            path = r["file_path"]
        secs = round((r["duration_ms"] or 0) / 1000) or -1
        lines.append(f"#EXTINF:{secs},{', '.join(json.loads(r['artists']))} - {r['title']}")
        lines.append(path)
    return lines


def _write(path: Path, name: str, lines: list[str]) -> bool:
    text = "\n".join(["#EXTM3U", f"#PLAYLIST:{name}", *lines]) + "\n"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def write_all(db: DB, library_root: str) -> int:
    """Write/refresh every playlist file; remove the ones Songarr wrote that are no longer wanted."""
    folder = Path(library_root) / "Playlists"
    used: set[str] = set()
    keep: list[str] = []
    changed = 0
    for _key, name, ids in wanted(db):
        base = safe(name, 100)
        fname, n = base, 2
        while fname.lower() in used:  # two names that are the same once made safe for files
            fname, n = f"{base} ({n})", n + 1
        used.add(fname.lower())
        path = folder / f"{fname}.m3u8"
        lines = _entries(db, ids, folder)
        if not lines and not path.exists():
            continue  # nothing downloaded yet: don't create empty files
        changed += _write(path, name, lines)
        keep.append(path.name)
    before = db.setting(WRITTEN) or []
    for old in set(before) - set(keep):
        _remove(folder / old, folder)
    # Liked Songs files Songarr wrote under an older name (identified by the header Songarr writes,
    # so nothing else is ever touched).
    if folder.exists():
        for f in folder.glob(f"{LIKED_NAME}*.m3u8"):
            if f.name in keep:
                continue
            try:
                with f.open(encoding="utf-8") as fh:
                    header = [fh.readline().strip(), fh.readline().strip()]
            except OSError:
                continue
            if header[0] == "#EXTM3U" and header[1].startswith(f"#PLAYLIST:{LIKED_NAME}"):
                _remove(f, folder)
    if sorted(keep) != sorted(before):
        db.set_setting(WRITTEN, sorted(keep))
    return changed


def _remove(p: Path, folder: Path) -> None:
    """Delete a playlist file Songarr wrote (only .m3u8 files directly in the Playlists folder)."""
    if p.suffix == ".m3u8" and p.parent == folder:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.info("could not remove old playlist file %s: %s", p, e)
