"""Push notifications to the apps through Firebase Cloud Messaging (FCM, HTTP v1).

Needs a Firebase service account key at <data>/firebase-service-account.json (Firebase
console → Project settings → Service accounts → Generate new private key). Without it,
sending does nothing and the apps only see Jam invites while they're open.

The server signs a short JWT with the account's private key, trades it for a one-hour access
token at Google, and posts each message to the phones' registration tokens (which the apps
send after they start). Tokens Google reports as gone are forgotten.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .db import DB

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM = "https://fcm.googleapis.com"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_rs256(message: bytes, pem: str) -> bytes:
    from Cryptodome.Hash import SHA256
    from Cryptodome.PublicKey import RSA
    from Cryptodome.Signature import pkcs1_15
    return pkcs1_15.new(RSA.import_key(pem)).sign(SHA256.new(message))


class Push:
    def __init__(self, db: DB, data_dir: Path):
        self.db = db
        self.key_file = Path(data_dir) / "firebase-service-account.json"
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.key_file.is_file()

    def _account(self) -> dict:
        return json.loads(self.key_file.read_text(encoding="utf-8"))

    def _access_token(self, account: dict) -> str:
        with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            now = int(time.time())
            header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            claims = _b64(json.dumps({"iss": account["client_email"], "scope": SCOPE, "aud": account["token_uri"],
                                      "iat": now, "exp": now + 3600}).encode())
            signature = _b64(_sign_rs256(f"{header}.{claims}".encode(), account["private_key"]))
            body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                           "assertion": f"{header}.{claims}.{signature}"}).encode()
            with urllib.request.urlopen(urllib.request.Request(account["token_uri"], data=body), timeout=20) as r:
                tok = json.load(r)
            self._token = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
            return self._token[0]

    def send_to_users(self, user_ids: list[int], title: str, body: str, data: dict[str, str], channel: str = "jams") -> int:
        """Notify every signed-in phone of these profiles; returns how many accepted it. Never raises.
        [channel] is the app's notification channel: "jams" (invites ping) or "releases" (new music, quieter)."""
        if not self.configured or not user_ids:
            return 0
        rows = self.db.q(f"""SELECT id, push_token FROM devices WHERE push_token IS NOT NULL
                             AND user_id IN ({','.join('?' * len(user_ids))})""", user_ids)
        if not rows:
            return 0
        try:
            account = self._account()
            token = self._access_token(account)
        except (OSError, ValueError, KeyError) as e:
            log.warning("push notifications unavailable: %s", e)
            return 0
        url = f"{FCM}/v1/projects/{account['project_id']}/messages:send"
        sent = 0
        for row in rows:
            message = {"message": {
                "token": row["push_token"],
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in data.items()},
                "android": {"priority": "high", "notification": {
                    "channel_id": channel, "tag": data.get("jam") or data.get("album") or "songarr",
                    "default_sound": True, "default_vibrate_timings": True,  # an invite should ping, not arrive silently
                    "notification_priority": "PRIORITY_HIGH" if channel == "jams" else "PRIORITY_DEFAULT"}},
            }}
            req = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20):
                    sent += 1
            except urllib.error.HTTPError as e:
                with e:
                    detail = e.read()[:300].decode(errors="replace")
                if e.code == 404 or "UNREGISTERED" in detail:
                    self.db.run("UPDATE devices SET push_token = NULL WHERE id = ?", (row["id"],))  # app uninstalled
                else:
                    log.warning("push to device %s failed: %s %s", row["id"], e.code, detail)
            except OSError as e:
                log.warning("push to device %s failed: %s", row["id"], e)
        return sent

    def send_async(self, user_ids: list[int], title: str, body: str, data: dict[str, str], channel: str = "jams") -> None:
        threading.Thread(target=self._send_quietly, args=(user_ids, title, body, data, channel), daemon=True, name="push").start()

    def _send_quietly(self, *args) -> None:
        try:
            self.send_to_users(*args)
        finally:
            self.db.release()
