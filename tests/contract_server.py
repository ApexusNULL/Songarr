"""A disposable Songarr server for testing the apps against (fake YouTube, a fake Deezer search, temp data).

    .venv\\Scripts\\python.exe -m tests.contract_server [--port 18486]

Prints `READY <server url> <pairing code>` once listening; stops on Ctrl+C. The app's
test/api_contract_test.dart uses it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from songarr import discover
from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube, audio_file, spotify_track

# what Deezer would answer for the catalogue search the contract test makes
DEEZER = {
    "/search/track?q=Fresh%20Tune&limit=20": {"data": [
        {"id": 78, "title": "Fresh Tune", "artist": {"name": "Artist 78"}, "album": {"title": "Album 78"}, "duration": 200}]},
    "/track/78": {"id": 78, "title": "Fresh Tune", "isrc": "USNEW0000078", "artist": {"name": "Artist 78"},
                  "contributors": [{"name": "Artist 78"}], "album": {"id": 780, "title": "Album 78"}, "duration": 200},
}


def _deezer(path: str, ttl: float) -> dict:
    """Offline: Deezer answers the contract test's search; anything else is as if it can't be reached."""
    if path in DEEZER:
        return DEEZER[path]
    raise OSError(f"no network in tests: {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18486)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--audio", nargs="*", default=[], help="real audio files to serve instead of test tones")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="songarr-contract-"))
    discover._get = _deezer
    svc = Service(tmp / "data", 8484, youtube_factory=lambda s: FakeYouTube())
    db = svc.db
    songs = [spotify_track(i, title=f"Song {i}") for i in range(1, 6)] + [spotify_track(7)]
    db.upsert_tracks(songs)
    with db.tx() as c:  # the first profile likes songs 1-5 and has a Road Trip playlist (songs 1 and 7)
        c.executemany("INSERT INTO likes(user_id, track_id, created) VALUES (1, ?, ?)", [(t["id"], float(n)) for n, t in enumerate(songs[:5])])
        c.executemany("INSERT INTO track_sources(track_id, source) VALUES (?, 'like:1')", [(t["id"],) for t in songs[:5]])
        c.execute("INSERT INTO user_playlists(id, user_id, name, created, updated) VALUES ('uproad', 1, 'Road Trip', 0, 0)")
        c.executemany("INSERT INTO user_playlist_tracks VALUES ('uproad', ?, ?, 0)", [(0, songs[0]["id"]), (1, songs[5]["id"])])
        c.executemany("INSERT INTO track_sources(track_id, source) VALUES (?, 'up:uproad')", [(songs[0]["id"],), (songs[5]["id"],)])
    db.recompute_monitored()
    for i in (1, 2, 3):  # a few songs are "downloaded"
        f = tmp / f"song{i}.m4a"
        shutil.copy(args.audio[(i - 1) % len(args.audio)] if args.audio else audio_file("m4a"), f)
        svc.db.set_status(f"{i:022d}", "downloaded", file_path=str(f), file_size=f.stat().st_size)
    http = make_app_server(svc, args.host, args.port)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    pair_code, _ = svc.users.new_pairing_code(1)
    print(f"READY http://{args.host}:{args.port} {pair_code}", flush=True)
    try:
        sys.stdin.read()  # until the test runner closes stdin / Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        http.shutdown()
        svc.stop(wait=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
