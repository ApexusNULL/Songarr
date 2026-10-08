"""Family profiles, signed-in devices, one-time pairing codes and optional passwords.

An app signs in by scanning a QR code from the admin page. The QR holds the server address
and a one-time code (10 minutes, single use); the app trades it for a long random device
token. Only SHA-256 hashes of codes and tokens are stored, so a copied database can't sign in.

A profile can also have a sign-in name and password, for signing in without the PC at hand.
Passwords are stored as scrypt hashes (slow and memory-hard to guess), and wrong tries are
limited per name and per address, because the app server is reachable from the internet.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from .db import DB

PAIR_TTL = 600
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: easy to type if the camera fails


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1  # ~16 MB and ~50 ms per check
MIN_PASSWORD = 8
LOGIN_TRIES = 5  # wrong passwords for one name per 10 minutes, then it waits
MAX_CHECKS = 4  # password checks at once: a burst of sign-ins waits its turn instead of using up the memory
_checking = threading.BoundedSemaphore(MAX_CHECKS)
_dummy_hash: str | None = None


class SignInError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        want_b = bytes.fromhex(want)
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(want_b))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want_b)


def _dummy() -> str:
    """A hash to check unknown names against, so they take as long as known ones."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_hex(16))
    return _dummy_hash


def _clean_login(login: str) -> str:
    return " ".join(login.split())[:40]


class Users:
    def __init__(self, db: DB):
        self.db = db
        self._failures: dict[str, list[float]] = {}
        self._name_failures: dict[str, list[float]] = {}  # wrong passwords, by sign-in name
        self._lock = threading.Lock()

    # -- profiles -------------------------------------------------------------

    def list(self) -> list[dict]:
        out = []
        for u in self.db.q("SELECT * FROM users ORDER BY id"):
            d = dict(u)
            d["has_password"] = bool(d.pop("password_hash"))
            d["likes"] = self.db.one("SELECT COUNT(*) FROM likes WHERE user_id = ?", (u["id"],))[0]
            d["playlists"] = self.db.one("SELECT COUNT(*) FROM user_playlists WHERE user_id = ?", (u["id"],))[0]
            d["devices"] = [dict(x) for x in self.db.q(
                "SELECT id, name, created, last_seen FROM devices WHERE user_id = ? ORDER BY last_seen DESC", (u["id"],))]
            out.append(d)
        return out

    def create(self, name: str) -> int:
        name = name.strip()[:40]
        if not name:
            raise ValueError("Give the profile a name.")
        if self.db.one("SELECT 1 FROM users WHERE name = ?", (name,)):
            raise ValueError(f"There's already a profile called {name}.")
        return self.db.run_insert("INSERT INTO users(name, is_admin, created) VALUES (?, 0, ?)", (name, time.time()))

    def rename(self, user_id: int, name: str) -> None:
        name = name.strip()[:40]
        if not name:
            raise ValueError("Give the profile a name.")
        if self.db.one("SELECT 1 FROM users WHERE name = ? AND id != ?", (name, user_id)):
            raise ValueError(f"There's already a profile called {name}.")
        self.db.run("UPDATE users SET name = ? WHERE id = ?", (name, user_id))

    def delete(self, user_id: int) -> None:
        """Remove a profile, its devices, likes, plays, app playlists and requests (songs stay)."""
        u = self.db.one("SELECT is_admin FROM users WHERE id = ?", (user_id,))
        if u is None:
            raise ValueError("Unknown profile.")
        if u["is_admin"]:
            raise ValueError("The main profile can't be removed.")
        with self.db.tx() as c:
            for (pid,) in c.execute("SELECT id FROM user_playlists WHERE user_id = ?", (user_id,)).fetchall():
                c.execute("DELETE FROM user_playlist_tracks WHERE playlist_id = ?", (pid,))
                c.execute("DELETE FROM track_sources WHERE source = ?", (f"up:{pid}",))
            c.execute("DELETE FROM user_playlists WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM track_sources WHERE source IN (?, ?)", (f"req:{user_id}", f"like:{user_id}"))
            for table in ("devices", "pairing_codes", "likes", "plays", "requests", "podcast_follows", "episode_progress",
                          "recommendations", "podcast_prefs", "episode_holds", "playlist_order", "liked_order",
                          "artist_follows", "notifications"):
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.db.recompute_monitored()

    # -- passwords (optional) ------------------------------------------------------

    def set_password(self, user_id: int, login: str, password: str) -> None:
        login = _clean_login(login)
        if not login:
            raise ValueError("Choose a sign-in name.")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")
        if len(password) > 200:
            raise ValueError("That password is too long.")
        if self.db.one("SELECT 1 FROM users WHERE id = ?", (user_id,)) is None:
            raise ValueError("Unknown profile.")
        if self.db.one("SELECT 1 FROM users WHERE login = ? COLLATE NOCASE AND id != ?", (login, user_id)):
            raise ValueError(f"Someone else already signs in as {login}.")
        self.db.run("UPDATE users SET login = ?, password_hash = ? WHERE id = ?", (login, hash_password(password), user_id))

    def clear_password(self, user_id: int) -> None:
        """Back to QR codes only (devices already signed in stay signed in)."""
        self.db.run("UPDATE users SET login = NULL, password_hash = NULL WHERE id = ?", (user_id,))

    def account(self, user_id: int) -> dict:
        row = self.db.one("SELECT name, login, password_hash FROM users WHERE id = ?", (user_id,))
        return {"name": row["name"], "login": row["login"], "has_password": bool(row["password_hash"])}

    def _name_tries(self, key: str) -> int:
        with self._lock:
            return self._name_count(key, time.time())

    def _name_count(self, key: str, now: float) -> int:
        """Wrong passwords for [key] in the last 10 minutes (with the lock held)."""
        hits = [t for t in self._name_failures.get(key, []) if now - t < 600]
        self._name_failures[key] = hits
        return len(hits)

    def _start_try(self, key: str, ip: str, by_address: bool = True) -> float:
        """Count a password try as wrong before it's checked, so tries sent all at once can't all get
        past the limits while the slow checks run; _right_try takes it back. Returns its time."""
        now = time.time()
        with self._lock:
            if (by_address and self._address_blocked(ip, now)) or self._name_count(key, now) >= LOGIN_TRIES:
                raise SignInError("Too many wrong tries. Wait 10 minutes, then try again.", 429)
            self._name_failures.setdefault(key, []).append(now)
            self._failures.setdefault(ip, []).append(now)
        return now

    def _right_try(self, key: str, ip: str, at: float) -> None:
        with self._lock:
            self._name_failures.pop(key, None)
            hits = self._failures.get(ip, [])
            if at in hits:
                hits.remove(at)

    @staticmethod
    def _check(password: str, stored: str | None) -> bool:
        with _checking:
            return check_password(password[:200], stored or _dummy())  # unknown names take as long as known ones

    def sign_in(self, login: str, password: str, device_name: str, ip: str) -> tuple[str, int]:
        """Trade a sign-in name and password for (device token, user id)."""
        login = _clean_login(login)
        key = login.lower()
        at = self._start_try(key, ip)
        row = self.db.one("SELECT id, password_hash FROM users WHERE login = ? COLLATE NOCASE", (login,)) if login else None
        stored = row["password_hash"] if row is not None and row["password_hash"] else None
        if not (self._check(password, stored) and stored):
            raise SignInError("That name or password isn't right.")  # (counted already)
        self._right_try(key, ip, at)
        return self._new_device(row["id"], device_name, ip), row["id"]

    def change_password(self, user_id: int, login: str, current: str, new: str, ip: str) -> None:
        """From a signed-in app. Changing an existing password needs the current one."""
        stored = self.db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))["password_hash"]
        if stored:
            key = f"#{user_id}"
            at = self._start_try(key, ip, by_address=False)
            if not self._check(current, stored):
                raise SignInError("Your current password isn't right.", 403)
            self._right_try(key, ip, at)
        self.set_password(user_id, login, new)

    # -- pairing and devices ----------------------------------------------------

    def new_pairing_code(self, user_id: int) -> tuple[str, float]:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))  # ~50 bits, single use, 10 minutes
        expires = time.time() + PAIR_TTL
        with self.db.tx() as c:
            c.execute("DELETE FROM pairing_codes WHERE expires < ?", (time.time(),))
            c.execute("INSERT INTO pairing_codes(code_hash, user_id, expires) VALUES (?, ?, ?)", (_h(code), user_id, expires))
        return code, expires

    def _throttled(self, ip: str) -> bool:
        with self._lock:
            return self._address_blocked(ip, time.time())

    def _address_blocked(self, ip: str, now: float) -> bool:
        """Too many wrong tries from [ip], or from everywhere together (with the lock held)."""
        hits = [t for t in self._failures.get(ip, []) if now - t < 600]
        total = sum(len([t for t in v if now - t < 600]) for v in self._failures.values())
        self._failures[ip] = hits
        return len(hits) >= 10 or total >= 50

    def _fail(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def pair(self, code: str, device_name: str, ip: str) -> tuple[str, int] | None:
        """Trade a pairing code for (device token, user id); None if wrong, expired or rate-limited."""
        if self._throttled(ip):
            return None
        code = "".join(ch for ch in code.upper() if ch in _CODE_ALPHABET)
        with self.db.tx() as c:
            row = c.execute("SELECT user_id, expires FROM pairing_codes WHERE code_hash = ?", (_h(code),)).fetchone()
            if row is None or row["expires"] < time.time():
                self._fail(ip)
                return None
            c.execute("DELETE FROM pairing_codes WHERE code_hash = ?", (_h(code),))  # single use
        return self._new_device(row["user_id"], device_name, ip), row["user_id"]

    def _new_device(self, user_id: int, device_name: str, ip: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.run("INSERT INTO devices(user_id, name, token_hash, created, last_seen, last_ip) VALUES (?,?,?,?,?,?)",
                    (user_id, (device_name or "Device").strip()[:60], _h(token), time.time(), time.time(), ip))
        return token

    def authenticate(self, token: str, ip: str) -> dict | None:
        """The user a device token belongs to, or None."""
        if not token:
            return None
        row = self.db.one("""SELECT d.id AS device_id, d.last_seen, u.id, u.name, u.is_admin FROM devices d
                             JOIN users u ON u.id = d.user_id WHERE d.token_hash = ?""", (_h(token),))
        if row is None:
            return None
        if (row["last_seen"] or 0) < time.time() - 60:  # don't write on every request
            self.db.run("UPDATE devices SET last_seen = ?, last_ip = ? WHERE id = ?", (time.time(), ip, row["device_id"]))
        return dict(row)

    def revoke_device(self, device_id: int) -> None:
        self.db.run("DELETE FROM devices WHERE id = ?", (device_id,))

    def revoke_token(self, token: str) -> None:
        self.db.run("DELETE FROM devices WHERE token_hash = ?", (_h(token),))
