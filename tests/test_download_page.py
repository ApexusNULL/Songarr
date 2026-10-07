"""The app's download page at <app address>/download."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from songarr.appapi import make_app_server
from songarr.service import Service

from tests.helpers import FakeYouTube


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DownloadPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir, free_port(), youtube_factory=lambda s: FakeYouTube())
        self.svc.db.set_setting("public_url", "https://music.private-example.net")
        self.svc.users.create("Someone Private")
        self.port = free_port()
        self.http = make_app_server(self.svc, "127.0.0.1", self.port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def publish(self, *abis: str) -> dict[str, bytes]:
        folder = self.dir / "app-updates"
        folder.mkdir(exist_ok=True)
        files, out = {}, {}
        for abi in abis:
            data = f"apk for {abi}".encode() * 100
            (folder / f"songarr-1.8.0+22-{abi}.apk").write_bytes(data)
            files[abi] = {"file": f"songarr-1.8.0+22-{abi}.apk", "version": "1.8.0", "build": 22, "notes": "",
                          "published": time.time(), "size": len(data), "sha256": "x"}
            out[abi] = data
        (folder / "manifest.json").write_text(json.dumps({"files": files}))
        return out

    def get(self, path: str) -> tuple[int, dict, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_the_page(self):
        self.publish("arm64-v8a", "armeabi-v7a")
        status, headers, body = self.get("/download")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("noindex", headers["X-Robots-Tag"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("Download for Android", page)
        self.assertIn("/download/Songarr-1.8.0.apk?abi=arm64-v8a", page)
        self.assertIn("abi=armeabi-v7a", page)  # older phones
        self.assertIn("Auto Blocker", page)
        for n in range(1, 7):
            self.assertRegex(page, rf'src="/download/img/{n}-[a-z-]+\.webp"')
        # nothing about the server or the people on it
        self.assertNotIn("private-example", page)
        self.assertNotIn("Someone Private", page)

    def test_the_app_and_pictures(self):
        apks = self.publish("arm64-v8a", "armeabi-v7a")
        status, headers, body = self.get("/download/Songarr-1.8.0.apk?abi=arm64-v8a")
        self.assertEqual((status, body), (200, apks["arm64-v8a"]))
        self.assertEqual(headers["Content-Type"], "application/vnd.android.package-archive")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Songarr-1.8.0.apk"')
        self.assertEqual(self.get("/download/x.apk?abi=armeabi-v7a")[2], apks["armeabi-v7a"])
        self.assertEqual(self.get("/download/x.apk?abi=mips")[0], 404)
        status, headers, body = self.get("/download/img/3-allow.webp")
        self.assertEqual((status, headers["Content-Type"], body[:4]), (200, "image/webp", b"RIFF"))
        for bad in ("/download/img/nope.webp", "/download/img/..%2f..%2fdb.webp", "/download/../api/v1/me", "/download/icon.png"):
            self.assertEqual(self.get(bad)[0], 404, bad)  # icon.png: Songarr's own icon is drawn in the page

    def test_nothing_published_yet(self):
        status, _, body = self.get("/download")
        self.assertEqual(status, 200)
        self.assertIn("isn't ready to download yet", body.decode())
        self.assertEqual(self.get("/download/Songarr-1.8.0.apk")[0], 404)

    def test_switched_off(self):
        self.publish("arm64-v8a")
        self.svc.db.set_setting("download_page", False)
        for path in ("/download", "/download/Songarr-1.8.0.apk", "/download/img/1-downloads.webp"):
            status, _, body = self.get(path)
            self.assertEqual((status, body), (404, b""), path)  # like any other page

    def test_other_pages_stay_empty(self):
        for path in ("/", "/downloads", "/admin", "/download.html"):
            status, _, body = self.get(path)
            self.assertEqual((status, body), (404, b""), path)


if __name__ == "__main__":
    unittest.main()
