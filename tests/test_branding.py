"""The name and icon (Settings → Name and icon): pictures turned into every icon Songarr needs, the
admin website and the apps showing them, the Android app rebuilt with them, and the installer's
command line."""

from __future__ import annotations

import base64
import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from songarr.__main__ import main
from songarr.app_build import OVERLAY, bump_build
from songarr.appapi import make_app_server
from songarr.branding import DEFAULT_ICON, BrandError, render
from songarr.db import DB
from songarr.service import Service
from songarr.web import make_server

from tests.helpers import FakeYouTube


def picture(size=(300, 200), color=(200, 40, 90, 255), fmt="PNG", see_through=False) -> bytes:
    img = Image.new("RGBA", size, (0, 0, 0, 0) if see_through else color)
    if see_through:
        img.paste(Image.new("RGBA", (size[0] // 2, size[1] // 2), color), (size[0] // 4, size[1] // 4))
    out = io.BytesIO()
    (img.convert("RGB") if fmt == "JPEG" else img).save(out, fmt)
    return out.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RenderTests(unittest.TestCase):
    def test_every_icon_from_one_picture(self):
        files = render(picture(fmt="JPEG"))  # 300 × 200: fitted into a square, not cropped
        for size in (16, 32, 192, 512):
            with Image.open(io.BytesIO(files[f"icon-{size}.png"])) as im:
                self.assertEqual(im.size, (size, size))
                if size >= 192:
                    self.assertEqual(im.getpixel((size // 2, 2))[3], 0)  # the band above the wide picture is see-through
        with Image.open(io.BytesIO(files["icon.ico"])) as ico:
            self.assertIn((16, 16), ico.info["sizes"])
            self.assertIn((256, 256), ico.info["sizes"])
        with Image.open(io.BytesIO(files["android-xxxhdpi-foreground.png"])) as fg:
            self.assertEqual(fg.size, (432, 432))
        self.assertEqual(files["see-through"], b"1")

    def test_a_full_square_picture(self):
        self.assertEqual(render(picture(size=(512, 512)))["see-through"], b"0")

    def test_not_a_picture(self):
        with self.assertRaises(BrandError):
            render(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        with self.assertRaises(BrandError):
            render(picture(size=(8, 8)))


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.b = self.svc.branding
        self.told = []
        self.b.listeners.append(lambda: self.told.append(self.b.name()))

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_songarr_until_changed(self):
        self.assertEqual(self.b.info(), {"name": "Songarr", "icon": None, "background": "#8B5CF6", "see_through": False,
                                         "default_name": "Songarr", "custom": False})
        self.assertEqual(self.b.ico(), DEFAULT_ICON)
        self.assertIsNone(self.b.png(64))

    def test_name_and_icon(self):
        info = self.b.set(name="  Harmony   Hub ", icon=picture(see_through=True), background="#112233")
        self.assertEqual(info["name"], "Harmony Hub")
        self.assertTrue(info["icon"] and info["see_through"] and info["custom"])
        self.assertEqual(info["background"], "#112233")
        self.assertEqual(self.b.png(100).name, "icon-128.png")  # the nearest size up
        self.assertEqual(self.b.ico().name, "icon.ico")
        self.assertEqual(self.told, ["Harmony Hub"])  # the tray and shortcuts are told once
        self.assertIn('name "Harmony Hub"', self.svc.db.one("SELECT message FROM history WHERE event = 'branding'")[0])
        self.b.set(name="Harmony Hub")  # nothing changed: nobody's told
        self.assertEqual(len(self.told), 1)
        old = info["icon"]
        self.b.set(reset_icon=True)
        self.assertIsNone(self.b.info()["icon"])
        self.assertEqual(self.b.ico(), DEFAULT_ICON)
        self.b.tidy()
        self.assertFalse((self.b.dir / old).exists())

    def test_bad_names_and_colours(self):
        for bad in ("", "   ", "x" * 31, "Mine<script>", "a/b"):
            with self.assertRaises(BrandError):
                self.b.set(name=bad)
        with self.assertRaises(BrandError):
            self.b.set(background="purple")
        self.assertEqual(self.b.name(), "Songarr")

    def test_android_resources(self):
        res = self.dir / "res"
        self.b.write_android(res)
        self.assertFalse(res.exists())  # Songarr's own: the built-in name and icon
        self.b.set(name="Tom & Jerry's")
        self.b.write_android(res)
        self.assertIn("<string name=\"app_name\">Tom &amp; Jerry's</string>", (res / "values" / "brand.xml").read_text(encoding="utf-8"))
        self.assertFalse((res / "mipmap-hdpi").exists())
        self.b.set(icon=picture())
        self.b.write_android(res)
        self.assertTrue((res / "mipmap-xxxhdpi" / "ic_launcher_brand.png").is_file())
        self.assertIn("@mipmap/ic_launcher_brand", (res / "mipmap-anydpi-v26" / "ic_launcher.xml").read_text(encoding="utf-8"))
        self.assertIn("brand_icon_background", (res / "values" / "brand.xml").read_text(encoding="utf-8"))


class WebsiteAndAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.port, cls.app_port = free_port(), free_port()
        cls.svc = Service(cls.dir / "data", cls.port, youtube_factory=lambda s: FakeYouTube())
        cls.http = make_server(cls.svc, "127.0.0.1", cls.port)
        cls.app_http = make_app_server(cls.svc, "127.0.0.1", cls.app_port)
        for server in (cls.http, cls.app_http):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        code, _ = cls.svc.users.new_pairing_code(1)
        cls.token = cls.app("POST", "/api/v1/auth/pair", {"code": code, "device": "Test phone"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.http, cls.app_http):
            server.shutdown()
            server.server_close()
        cls.svc.stop(wait=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def request(cls, url, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    @classmethod
    def admin(cls, method, path, body=None):
        return cls.request(f"http://127.0.0.1:{cls.port}{path}", method, body, {"X-Songarr": "1"})

    @classmethod
    def app(cls, method, path, body=None, token=None):
        status, data, _ = cls.request(f"http://127.0.0.1:{cls.app_port}{path}", method, body,
                                      {"Authorization": f"Bearer {token}"} if token else {})
        return status, json.loads(data) if data and data[:1] in b"{[" else data

    def tearDown(self):
        self.svc.branding.set(name="Songarr", reset_icon=True)

    def test_admin_page_and_app_follow_the_name_and_icon(self):
        status, page, _ = self.admin("GET", "/")
        self.assertIn(b"<title>Songarr</title>", page)
        self.assertEqual(self.admin("GET", "/favicon.ico")[1], DEFAULT_ICON.read_bytes())
        self.assertEqual(self.admin("GET", "/brand/icon-32.png")[0], 404)

        status, body, _ = self.admin("POST", "/api/branding", {"name": "Harmony <Hub>"})
        self.assertEqual(status, 400)  # no angle brackets
        status, body, _ = self.admin("POST", "/api/branding", {"name": "Tom & Jerry", "icon": base64.b64encode(picture()).decode()})
        self.assertEqual(status, 200, body)
        info = json.loads(body)
        self.assertEqual(info["name"], "Tom & Jerry")
        self.assertIn("ready", info["app"])  # can this PC rebuild the phone app?

        page = self.admin("GET", "/")[1].decode()
        self.assertIn("<title>Tom &amp; Jerry</title>", page)
        self.assertIn(f'href="/brand/icon-32.png?v={info["icon"]}"', page)
        self.assertIn('"name": "Tom & Jerry"', page)  # BRAND, for the page's own text
        status, png, headers = self.admin("GET", "/brand/icon-32.png")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertTrue(self.admin("GET", "/favicon.ico")[1].startswith(b"\x00\x00\x01\x00"))
        self.assertEqual(json.loads(self.admin("GET", "/api/status")[1])["brand"]["name"], "Tom & Jerry")

        me = self.app("GET", "/api/v1/me", token=self.token)[1]
        self.assertEqual((me["server"]["name"], me["server"]["icon"]), ("Tom & Jerry", info["icon"]))
        status, icon = self.app("GET", "/api/v1/branding/icon?size=192", token=self.token)
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(icon)) as im:
            self.assertEqual(im.size, (192, 192))
        self.assertEqual(self.app("GET", "/api/v1/branding/icon")[0], 401)  # only for signed-in devices

    def test_a_broken_upload(self):
        status, body, _ = self.admin("POST", "/api/branding", {"icon": "not base64!"})
        self.assertEqual(status, 400)
        status, body, _ = self.admin("POST", "/api/branding", {"icon": base64.b64encode(b"hello").decode()})
        self.assertEqual(status, 400)
        self.assertIn("isn't a picture", json.loads(body)["error"])


class AppBuildTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.svc = Service(self.dir / "data", 8484, youtube_factory=lambda s: FakeYouTube())
        self.app = self.dir / "app"
        (self.app / "android").mkdir(parents=True)
        (self.app / "pubspec.yaml").write_text("name: songarr_app\nversion: 1.6.0+16\n", encoding="utf-8")
        self.svc.db.set_setting("app_source_dir", str(self.app))
        self.svc.db.set_setting("flutter_path", str(self.dir / "flutter.bat"))
        (self.dir / "flutter.bat").write_text("@echo off\n")
        self.build = self.svc.app_build
        self.builds = []

        def fake_flutter(app, name):  # what `flutter build apk --split-per-abi` leaves behind
            self.builds.append(name)
            out = app / "build" / "app" / "outputs" / "flutter-apk"
            out.mkdir(parents=True, exist_ok=True)
            (out / "app-arm64-v8a-release.apk").write_bytes(b"PK fake apk")
            return True, "Built app-arm64-v8a-release.apk"
        self.build.run_build = fake_flutter

    def tearDown(self):
        self.svc.stop(wait=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def wait(self):
        for _ in range(200):
            if not self.build.state["running"]:
                return
            threading.Event().wait(0.05)
        self.fail("the build didn't finish")

    def test_needs_the_signing_key(self):
        check = self.build.check()
        self.assertFalse(check["ready"])
        self.assertIn("signing key", " ".join(check["problems"]))
        with self.assertRaises(ValueError):
            self.build.start()

    def test_rebuilds_and_offers_it_to_phones(self):
        (self.app / "android" / "key.properties").write_text("storeFile=x\n")
        self.assertFalse(self.build.check()["needed"])  # Songarr's own name and icon: nothing to do
        self.svc.branding.set(name="Harmony", icon=picture())
        self.assertTrue(self.build.check()["needed"])
        self.build.start()
        self.wait()
        self.assertTrue(self.build.state["ok"], self.build.state)
        self.assertEqual(self.builds, ["Harmony"])
        self.assertIn("version: 1.6.0+17", (self.app / "pubspec.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.app / OVERLAY / "mipmap-hdpi" / "ic_launcher.png").is_file())
        latest = self.svc.app_updates.latest("arm64-v8a")
        self.assertEqual((latest["version"], latest["build"]), ("1.6.0", 17))
        self.assertIn("Harmony", latest["notes"])
        self.assertFalse(self.build.check()["needed"])

    def test_build_numbers_only_go_up(self):
        (self.app / "pubspec.yaml").write_text("version: 1.6.0+16\n", encoding="utf-8")
        self.assertEqual(bump_build(self.app, 20), ("1.6.0", 21))  # past what phones were already offered
        self.assertEqual(bump_build(self.app, 3), ("1.6.0", 22))


class CommandLineTests(unittest.TestCase):
    """What the installer runs: python -m songarr --data <folder> --brand-name ... --brand-icon ..."""

    def test_sets_the_name_and_icon(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "logo.png").write_bytes(picture())
            self.assertEqual(main(["--data", str(tmp / "data"), "--brand-name", "Family Tunes", "--brand-icon", str(tmp / "logo.png")]), 0)
            db = DB(tmp / "data" / "songarr.db")
            self.assertEqual(db.setting("brand_name"), "Family Tunes")
            self.assertTrue((tmp / "data" / "branding" / db.setting("brand_icon") / "icon.ico").is_file())
            db.close()
            self.assertEqual(main(["--data", str(tmp / "data"), "--brand-name", "bad/name"]), 2)
            self.assertEqual(main(["--data", str(tmp / "data"), "--music-folder", str(tmp / "My Music")]), 0)
            db = DB(tmp / "data" / "songarr.db")
            self.assertEqual(db.setting("library_root"), str(tmp / "My Music"))
            self.assertTrue((tmp / "My Music").is_dir())
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stops_a_running_songarr(self):
        """Before an update or uninstalling, the installer stops Songarr through its admin website."""
        tmp = Path(tempfile.mkdtemp())
        port = free_port()
        svc = Service(tmp / "data", port, youtube_factory=lambda s: FakeYouTube())
        http = make_server(svc, "127.0.0.1", port)

        def serve():  # as __main__ does: the ports close once it's asked to stop
            http.serve_forever(poll_interval=0.1)
            http.server_close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.assertEqual(main(["--stop", "--port", str(port)]), 0)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(main(["--stop", "--port", str(port)]), 0)  # not running: nothing to do
        finally:
            svc.stop(wait=True)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
