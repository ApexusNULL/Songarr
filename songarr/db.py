"""SQLite storage: tracks, match candidates, history and settings. One connection per thread."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tracks(
    id            TEXT PRIMARY KEY,           -- Spotify's track id (Exportify files carry it), or dz<number> for Deezer
    title         TEXT NOT NULL,
    artists       TEXT NOT NULL,              -- JSON list
    album         TEXT,
    album_artists TEXT,                       -- JSON list
    album_id      TEXT,
    release_date  TEXT,
    track_number  INTEGER,
    disc_number   INTEGER,
    duration_ms   INTEGER,
    isrc          TEXT,
    explicit      INTEGER NOT NULL DEFAULT 0,
    cover_url     TEXT,
    thumb_url     TEXT,
    added_at      TEXT,                       -- when it was first liked (on Spotify, for songs brought over)
    monitored     INTEGER NOT NULL DEFAULT 1, -- 0 once un-liked (the file is kept)
    status        TEXT NOT NULL DEFAULT 'wanted',
    pinned        INTEGER NOT NULL DEFAULT 0, -- youtube_id was chosen by hand
    youtube_id    TEXT,
    match_score   REAL,
    file_path     TEXT,
    file_size     INTEGER,
    bitrate       REAL,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt  REAL,
    updated       REAL
);
CREATE INDEX IF NOT EXISTS tracks_status ON tracks(status, added_at);

CREATE TABLE IF NOT EXISTS candidates(
    track_id   TEXT NOT NULL,
    youtube_id TEXT NOT NULL,
    title      TEXT,
    channel    TEXT,
    duration   REAL,
    score      REAL,
    source     TEXT,
    PRIMARY KEY(track_id, youtube_id)
);

-- Where each track comes from. A track is monitored (kept downloaded) while it has any source:
--   req:<user id>       requested from the app
--   like:<user id>      in that person's Liked Songs
--   up:<playlist id>    in one of their playlists
CREATE TABLE IF NOT EXISTS track_sources(
    track_id TEXT NOT NULL,
    source   TEXT NOT NULL,
    PRIMARY KEY(track_id, source)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS track_sources_source ON track_sources(source);

-- People using Songarr (family profiles). Songs are shared; likes, playlists and plays are per person.
CREATE TABLE IF NOT EXISTS users(
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created  REAL NOT NULL
);

-- Apps signed in as a user. Only a SHA-256 of each token is stored.
CREATE TABLE IF NOT EXISTS devices(
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    name       TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    created    REAL NOT NULL,
    last_seen  REAL,
    last_ip    TEXT
);

-- One-time codes shown as a QR in the admin page to sign an app in (hashed, 10-minute life).
CREATE TABLE IF NOT EXISTS pairing_codes(
    code_hash TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    expires   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS likes(
    user_id  INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    created  REAL NOT NULL,
    PRIMARY KEY(user_id, track_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS plays(
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    track_id  TEXT NOT NULL,
    played_at REAL NOT NULL,
    ms_played INTEGER,
    completed INTEGER
);
CREATE INDEX IF NOT EXISTS plays_user ON plays(user_id, played_at DESC);

CREATE TABLE IF NOT EXISTS user_playlists(
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);

-- Each person's own order for their Liked Songs, once they've moved something (new likes go first).
CREATE TABLE IF NOT EXISTS liked_order(
    user_id  INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY(user_id, track_id)
) WITHOUT ROWID;

-- Artists people follow (releases.py), their releases on Deezer, and the notifications about them.
CREATE TABLE IF NOT EXISTS artists(
    deezer_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    image_url TEXT,
    checked   REAL                            -- when their releases were last looked at (NULL: never)
);
CREATE TABLE IF NOT EXISTS artist_follows(
    user_id     INTEGER NOT NULL,
    deezer_id   INTEGER NOT NULL,
    followed_at REAL NOT NULL,
    PRIMARY KEY(user_id, deezer_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS artist_releases(
    album_id         INTEGER NOT NULL,
    deezer_artist_id INTEGER NOT NULL,
    title            TEXT,
    release_date     TEXT,
    record_type      TEXT,                    -- album, ep, single, compile
    cover_url        TEXT,
    thumb_url        TEXT,
    explicit         INTEGER NOT NULL DEFAULT 0,
    first_seen       REAL NOT NULL,
    PRIMARY KEY(album_id, deezer_artist_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS artist_releases_date ON artist_releases(deezer_artist_id, release_date);
CREATE TABLE IF NOT EXISTS notifications(
    id      INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind    TEXT NOT NULL,                    -- release
    title   TEXT NOT NULL,
    body    TEXT,
    data    TEXT,                             -- JSON: what tapping it opens
    created REAL NOT NULL,
    read    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, created);

-- Audio files found in the music folder (scan.py), so songs already there aren't downloaded again.
CREATE TABLE IF NOT EXISTS library_files(
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    title      TEXT,
    artist     TEXT,
    album      TEXT,
    isrc       TEXT,
    spotify_id TEXT,                          -- Songarr's own tag: the file was made by Songarr
    duration   REAL,
    title_key  TEXT,                          -- the title without "(feat. ...)", "- Remastered", punctuation
    seen       REAL
);
CREATE INDEX IF NOT EXISTS library_files_isrc ON library_files(isrc);
CREATE INDEX IF NOT EXISTS library_files_spotify ON library_files(spotify_id);
CREATE INDEX IF NOT EXISTS library_files_title ON library_files(title_key);

-- Each person's own order for the playlists in their library (playlists not in it, e.g. new
-- ones, come first).
CREATE TABLE IF NOT EXISTS playlist_order(
    user_id     INTEGER NOT NULL,
    playlist_id TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY(user_id, playlist_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS user_playlist_tracks(
    playlist_id TEXT NOT NULL,
    position    INTEGER NOT NULL,
    track_id    TEXT NOT NULL,
    added_at    REAL,
    PRIMARY KEY(playlist_id, position)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS requests(
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    created  REAL NOT NULL,
    UNIQUE(user_id, track_id)
);

CREATE TABLE IF NOT EXISTS history(
    id       INTEGER PRIMARY KEY,
    time     REAL NOT NULL,
    track_id TEXT,
    event    TEXT NOT NULL,
    message  TEXT
);
CREATE INDEX IF NOT EXISTS history_time ON history(time DESC);

-- Podcasts come from the Apple Podcasts directory and each show's public RSS feed.
CREATE TABLE IF NOT EXISTS podcasts(
    id          TEXT PRIMARY KEY,             -- 'ap' + Apple Podcasts id
    title       TEXT NOT NULL,
    author      TEXT,
    artwork_url TEXT,
    feed_url    TEXT,
    genre       TEXT,
    genre_ids   TEXT,                         -- JSON list of Apple genre ids
    description TEXT,
    website     TEXT,
    fetched     REAL                          -- last time the feed was read
);

CREATE TABLE IF NOT EXISTS podcast_episodes(
    id          TEXT PRIMARY KEY,             -- 'ep' + hash of podcast id and the episode's guid
    podcast_id  TEXT NOT NULL,
    guid        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    published   REAL,
    duration_ms INTEGER,
    audio_url   TEXT NOT NULL,
    audio_type  TEXT,
    image_url   TEXT
);
CREATE INDEX IF NOT EXISTS episodes_by_podcast ON podcast_episodes(podcast_id, published DESC);

-- Shows people follow.
CREATE TABLE IF NOT EXISTS podcast_follows(
    user_id    INTEGER NOT NULL,
    podcast_id TEXT NOT NULL,
    created    REAL NOT NULL,
    PRIMARY KEY(user_id, podcast_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS episode_progress(
    user_id     INTEGER NOT NULL,
    episode_id  TEXT NOT NULL,
    position_ms INTEGER NOT NULL,
    duration_ms INTEGER,
    completed   INTEGER NOT NULL DEFAULT 0,
    updated     REAL NOT NULL,
    PRIMARY KEY(user_id, episode_id)
) WITHOUT ROWID;

-- Podcast downloads expire (unlike songs): each profile chooses per show to keep new
-- episodes until they're played or for some days; single episodes can be kept the same way.
CREATE TABLE IF NOT EXISTS podcast_prefs(
    user_id    INTEGER NOT NULL,
    podcast_id TEXT NOT NULL,
    keep       TEXT NOT NULL,                -- 'off', 'played', or a number of days
    PRIMARY KEY(user_id, podcast_id)
) WITHOUT ROWID;

-- An episode someone wants downloaded, until it's played or the days run out (then `released`).
CREATE TABLE IF NOT EXISTS episode_holds(
    user_id    INTEGER NOT NULL,
    episode_id TEXT NOT NULL,
    keep       TEXT NOT NULL,
    created    REAL NOT NULL,
    released   REAL,
    auto       INTEGER NOT NULL DEFAULT 0,       -- from the show's setting, not picked by hand
    PRIMARY KEY(user_id, episode_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS episode_files(
    episode_id TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    size       INTEGER,
    downloaded REAL NOT NULL
);

-- Artist bios from Wikipedia/Wikidata, cached (data is "null" for artists without a page).
CREATE TABLE IF NOT EXISTS artist_info(
    name_key TEXT PRIMARY KEY,
    data     TEXT NOT NULL,
    fetched  REAL NOT NULL
) WITHOUT ROWID;

-- Lyrics from LRCLIB, cached per song (data is "null" when none were found).
CREATE TABLE IF NOT EXISTS lyrics(
    track_id TEXT PRIMARY KEY,
    data     TEXT NOT NULL,
    fetched  REAL NOT NULL
) WITHOUT ROWID;

-- Per-profile "made for you" songs, artists and podcasts, rebuilt in the background.
CREATE TABLE IF NOT EXISTS recommendations(
    user_id  INTEGER PRIMARY KEY,
    data     TEXT NOT NULL,
    computed REAL NOT NULL
);
"""

# wanted -> searching -> downloading -> downloaded
#                    \-> review (no confident match: pick one in the UI)
#                    \-> failed (retried later with backoff)
# ignored: never downloaded until you un-ignore it
STATUSES = ("wanted", "searching", "downloading", "downloaded", "review", "failed", "ignored")
BUSY = ("searching", "downloading")

DEFAULT_SETTINGS: dict[str, Any] = {
    "library_root": str(Path.home() / "Music" / "Songarr"),  # change it in Settings
    "audio_format": "m4a",
    "workers": 4,
    "cookies_file": "",
    "match_threshold": 0.70,
    "ffmpeg_path": "",
    "max_per_hour": 250,           # YouTube allows guests ~300 videos/hour; stay under it
    "write_playlist_files": True,  # .m3u8 files in <library>/Playlists
    "public_url": "",              # address apps use, e.g. your Cloudflare Tunnel hostname
    "download_page": True,         # <app address>/download offers the Android app (download_page.py)
    "wikimedia_contact": "",       # email or URL sent to Wikipedia with bio requests (lifts its limit 10 -> 200/min)
    "lastfm_api_key": "",          # optional: bios from Last.fm for artists Wikipedia doesn't cover
    "auto_update": True,           # keep yt-dlp and the other packages up to date (dependencies.py)
    "brand_name": "Songarr",       # what the website, apps, tray icon and shortcuts are called (branding.py)
    "brand_icon": "",              # the chosen icon (a folder in <data>/branding); "" for Songarr's own
    "brand_background": "#8B5CF6", # behind the icon on Android home screens
    "app_source_dir": "",          # the Android app's source, for rebuilding it with the name and icon ("" = beside the server)
    "flutter_path": "",            # flutter.bat, when it isn't on the PATH
}

# Columns added after the first release, applied to existing databases on start.
MIGRATIONS = {
    "tracks": {"blocked": "INTEGER NOT NULL DEFAULT 0",
               "priority": "INTEGER NOT NULL DEFAULT 0",  # app requests jump the queue
               "requested_at": "REAL",  # when it was last requested: the newest request downloads first
               "previous_youtube_id": "TEXT"},  # while another version replaces it: the upload it had (service.replace)
    "devices": {"push_token": "TEXT"},  # Firebase registration token, for Jam invites
    "user_playlists": {"source_id": "TEXT",  # the Spotify playlist it was brought over from (before Exportify)
                       "source_name": "TEXT", "source_snapshot": "TEXT", "source_seen": "TEXT", "image_url": "TEXT"},
    "users": {"login": "TEXT",  # optional: sign in by name and password instead of a QR code
              "password_hash": "TEXT"},  # scrypt, see users.hash_password
}

# Settings that held the single Spotify connection before family profiles existed (Songarr no longer
# talks to Spotify; they're removed on start).
_OLD_SPOTIFY_KEYS = ("spotify_access_token", "spotify_refresh_token", "spotify_expires_at", "spotify_user",
                     "spotify_user_id", "spotify_scopes")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # With a backup server (cluster.py), each server keeps a few settings to itself (its music
        # folder, its app address...): these win over the shared database, and saving one saves it there.
        self.local: dict[str, Any] = {}
        self.local_keys: tuple[str, ...] = ()
        self.save_local: Callable[[dict], None] | None = None
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self.conn.executescript(SCHEMA)  # manages its own transaction
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_login ON users(login COLLATE NOCASE)")
        # requests from before requested_at existed: their request time
        self.conn.execute("""UPDATE tracks SET requested_at = (SELECT MAX(created) FROM requests r WHERE r.track_id = tracks.id)
                             WHERE priority > 0 AND requested_at IS NULL""")
        self._migrate_to_profiles()
        self._retire_spotify()

    def _migrate_to_profiles(self) -> None:
        """First start (or the first with family profiles): create the first (admin) profile; the
        Liked Songs of the single Spotify connection the earliest versions had become its likes."""
        if self.one("SELECT 1 FROM users LIMIT 1"):
            return
        name = (self.setting("spotify_user") or "Me").strip() or "Me"
        with self.tx() as c:
            uid = c.execute("INSERT INTO users(name, is_admin, created) VALUES (?, 1, ?)", (name, time.time())).lastrowid
            c.execute("UPDATE track_sources SET source = ? WHERE source = 'liked'", (f"like:{uid}",))
            c.execute("INSERT OR IGNORE INTO likes(user_id, track_id, created) SELECT ?, track_id, ? FROM track_sources "
                      "WHERE source = ?", (uid, time.time(), f"like:{uid}"))
            c.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in _OLD_SPOTIFY_KEYS])

    def _retire_spotify(self) -> None:
        """Songarr no longer talks to Spotify: people bring their library over from an Exportify file
        (exportify.py). What linked Spotify accounts brought in becomes each person's own, as if they'd
        done it in the app: their Liked Songs become their likes (liked at the same moment, so the order
        stays), and podcasts saved on Spotify become podcasts they follow. (Their Spotify playlists
        already are their own playlists.) Then the Spotify tables, sign-ins and settings go. Runs once;
        the database from before is kept beside it, as songarr-before-spotify-removal.db."""
        tables = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "spotify_accounts" not in tables:
            return
        keep = Path(self.path).with_name("songarr-before-spotify-removal.db")
        if not keep.exists():
            dst = sqlite3.connect(keep)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        now = time.time()
        unliked = "SELECT track_id FROM unlikes WHERE user_id = ?" if "unlikes" in tables else "SELECT NULL WHERE ? IS NULL"
        with self.tx() as c:
            liked = shows = 0
            for aid, uid in c.execute("SELECT id, user_id FROM spotify_accounts").fetchall():
                added = "pt.added_at" if "playlist_tracks" in tables else "NULL"
                joined = ("LEFT JOIN playlist_tracks pt ON pt.playlist_id = s.source AND pt.track_id = s.track_id"
                          if "playlist_tracks" in tables else "")
                liked += c.execute(f"""INSERT OR IGNORE INTO likes(user_id, track_id, created)
                                       SELECT ?, s.track_id, COALESCE(MIN(CAST(strftime('%s', {added}) AS REAL)), ?)
                                       FROM track_sources s {joined}
                                       WHERE s.source = ? AND s.track_id NOT IN ({unliked})
                                       GROUP BY s.track_id""", (uid, now, f"liked:{aid}", uid)).rowcount
                if "account_shows" in tables:
                    shows += c.execute("""INSERT OR IGNORE INTO podcast_follows(user_id, podcast_id, created)
                                          SELECT ?, podcast_id, ? FROM account_shows WHERE account_id = ?""",
                                       (uid, now, aid)).rowcount
            c.execute("INSERT OR IGNORE INTO track_sources(track_id, source) SELECT track_id, 'like:' || user_id FROM likes")
            c.execute("DELETE FROM track_sources WHERE source GLOB 'liked:*' OR source GLOB 'pl:*'")
            if "playlists" in tables:  # the playlist files Songarr wrote for them: the next write tidies them up
                old = [Path(r[0]).name for r in c.execute("SELECT m3u_path FROM playlists WHERE m3u_path IS NOT NULL")]
                if old:
                    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('m3u_files', ?)", (json.dumps(old),))
            for t in ("spotify_accounts", "account_playlists", "account_shows", "spotify_shows", "playlist_dismissed",
                      "unlikes", "playlists", "playlist_tracks"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
            c.execute("DELETE FROM settings WHERE key GLOB 'spotify_*' OR key IN ('sync_new_playlists', 'sync_interval_minutes')")
            c.execute("INSERT INTO history(time, event, message) VALUES (?, 'system', ?)",
                      (now, f"Songarr no longer reads Spotify (Exportify brings libraries over): {liked} liked song"
                            f"{'' if liked == 1 else 's'} and {shows} saved podcast{'' if shows == 1 else 's'} from linked "
                            f"Spotify accounts are now people's own. The database from before is kept as {keep.name}."))
        self.recompute_monitored()

    def liked_list(self, user_id: int) -> list[str]:
        """A person's Liked Songs in their order: newest like first, unless they've arranged them
        (then songs liked since come first, newest first, followed by their arrangement)."""
        ids = [r[0] for r in self.q("SELECT track_id FROM likes WHERE user_id = ? ORDER BY created DESC", (user_id,))]
        order = {r[0]: r[1] for r in self.q("SELECT track_id, position FROM liked_order WHERE user_id = ?", (user_id,))}
        if order:
            ids = [i for i in ids if i not in order] + sorted((i for i in ids if i in order), key=order.__getitem__)
        return ids

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # One connection per thread; check_same_thread=False only so close() can run after threads end.
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            with self._all_lock:
                self._all.append(c)
        return c

    def release(self) -> None:
        """Close this thread's connection. Short-lived threads (one per web request) must call this."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            self._local.conn = None
            with self._all_lock:
                if c in self._all:
                    self._all.remove(c)
            c.close()

    def close(self) -> None:
        """Close every thread's connection; call only once workers have stopped."""
        with self._all_lock:
            for c in self._all:
                c.close()
            self._all.clear()
        self._local = threading.local()

    class _Tx:
        def __init__(self, conn: sqlite3.Connection):
            self.c = conn

        def __enter__(self) -> sqlite3.Connection:
            self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, exc_type, exc, tb) -> None:
            self.c.execute("ROLLBACK" if exc_type else "COMMIT")

    def tx(self) -> "DB._Tx":
        return DB._Tx(self.conn)

    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple | list = ()) -> int:
        return self.conn.execute(sql, params).rowcount

    def run_insert(self, sql: str, params: tuple | list = ()) -> int:
        return self.conn.execute(sql, params).lastrowid

    # -- settings ---------------------------------------------------------

    def setting(self, key: str) -> Any:
        if key in self.local:
            return self.local[key]
        if self.save_local and key in self.local_keys:  # this server's own: never another server's
            return DEFAULT_SETTINGS.get(key)
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row[0]) if row else DEFAULT_SETTINGS.get(key)

    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for k, v in self.q("SELECT key, value FROM settings"):
            if not (self.save_local and k in self.local_keys):  # this server's own come from self.local
                out[k] = json.loads(v)
        return out | self.local

    def set_setting(self, key: str, value: Any) -> None:
        if self.save_local and key in self.local_keys:  # this server's own
            self.local[key] = value
            self.save_local(dict(self.local))
            return
        self.run("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))

    def del_setting(self, key: str) -> None:
        self.run("DELETE FROM settings WHERE key = ?", (key,))

    # -- history ------------------------------------------------------------

    def log(self, event: str, message: str, track_id: str | None = None) -> None:
        self.run("INSERT INTO history(time, track_id, event, message) VALUES (?, ?, ?, ?)",
                 (time.time(), track_id, event, message))

    # -- tracks -------------------------------------------------------------

    def upsert_tracks(self, tracks: list[dict]) -> int:
        """Insert or refresh metadata; returns how many tracks were new."""
        new = 0
        now = time.time()
        with self.tx() as c:
            for t in tracks:
                existed = c.execute("SELECT 1 FROM tracks WHERE id = ?", (t["id"],)).fetchone()
                c.execute(
                    """INSERT INTO tracks(id, title, artists, album, album_artists, album_id, release_date, track_number,
                                          disc_number, duration_ms, isrc, explicit, cover_url, thumb_url, added_at, updated)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         title=excluded.title, artists=excluded.artists, album=excluded.album,
                         album_artists=excluded.album_artists, album_id=excluded.album_id,
                         release_date=excluded.release_date, track_number=excluded.track_number,
                         disc_number=excluded.disc_number, duration_ms=excluded.duration_ms,
                         isrc=COALESCE(excluded.isrc, tracks.isrc), explicit=excluded.explicit,
                         cover_url=excluded.cover_url, thumb_url=excluded.thumb_url,
                         added_at=MAX(COALESCE(tracks.added_at, ''), COALESCE(excluded.added_at, ''))""",
                    (t["id"], t["title"], json.dumps(t["artists"]), t.get("album"), json.dumps(t.get("album_artists") or []),
                     t.get("album_id"), t.get("release_date"), t.get("track_number"), t.get("disc_number"),
                     t.get("duration_ms"), t.get("isrc"), int(bool(t.get("explicit"))), t.get("cover_url"),
                     t.get("thumb_url"), t.get("added_at"), now),
                )
                new += existed is None
        return new

    def add_source(self, track_id: str, source: str) -> None:
        self.run("INSERT OR IGNORE INTO track_sources(track_id, source) VALUES (?, ?)", (track_id, source))

    def remove_source(self, track_id: str, source: str) -> None:
        self.run("DELETE FROM track_sources WHERE track_id = ? AND source = ?", (track_id, source))

    def recompute_monitored(self) -> tuple[int, int]:
        """monitored = has a source (see track_sources). Returns (newly monitored, dropped).
        Files of dropped tracks are left alone."""
        live = "EXISTS(SELECT 1 FROM track_sources s WHERE s.track_id = tracks.id)"
        with self.tx() as c:
            added = c.execute(f"UPDATE tracks SET monitored = 1 WHERE monitored = 0 AND {live}").rowcount
            dropped = c.execute(f"UPDATE tracks SET monitored = 0 WHERE monitored = 1 AND NOT {live}").rowcount
        return added, dropped

    def claim_next(self) -> sqlite3.Row | None:
        """Atomically move the newest-liked eligible track to 'searching' and return it."""
        now = time.time()
        return self.one(
            """UPDATE tracks SET status = 'searching', error = NULL, updated = ?
               WHERE id = (SELECT id FROM tracks
                           WHERE monitored = 1 AND (status = 'wanted'
                              OR (status = 'failed' AND next_attempt IS NOT NULL AND next_attempt <= ?))
                           -- requests first, the newest request first (an album's songs in track order);
                           -- then the rest, the newest like first
                           ORDER BY priority DESC, requested_at DESC, added_at DESC LIMIT 1)
               RETURNING *""",
            (now, now),
        )

    def reset_busy(self) -> int:
        """After a crash or restart nothing is really in progress."""
        return self.run(f"UPDATE tracks SET status = 'wanted' WHERE status IN {BUSY}")

    def set_status(self, track_id: str, status: str, **fields: Any) -> None:
        fields["status"] = status
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE tracks SET {cols} WHERE id = ?", (*fields.values(), track_id))

    def save_candidates(self, track_id: str, cands: list) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM candidates WHERE track_id = ?", (track_id,))
            c.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?)",
                [(track_id, x.id, x.title, x.channel, x.duration, x.score, x.source) for x in cands],
            )

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for status, n in self.q("SELECT status, COUNT(*) FROM tracks WHERE monitored = 1 GROUP BY status"):
            out[status] = n
        out["unmonitored"] = self.one("SELECT COUNT(*) FROM tracks WHERE monitored = 0")[0]
        out["total"] = sum(out[s] for s in STATUSES)
        return out


def track_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["artists"] = json.loads(d["artists"] or "[]")
    d["album_artists"] = json.loads(d["album_artists"] or "[]")
    return d


REPLACING = ("wanted", "searching", "downloading")  # another version on its way (Service.replace)


def has_file(t) -> bool:
    """The song can be played: it's downloaded, or another version of it is on its way (Replace)
    and the file it had plays until then."""
    return t["status"] == "downloaded" or bool(t["pinned"] and t["file_path"] and t["status"] in REPLACING)
