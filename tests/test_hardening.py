"""Attacks from the internet: password guesses sent all at once, and outside addresses that turn
out to point into the home network (DNS rebinding). All offline."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from songarr import podcasts, users
from songarr.db import DB
from songarr.users import SignInError, Users

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = "93.184.216.34"  # stands in for a server on the internet (connections to it go to a test server here)


class ParallelPasswordTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.users = Users(DB(self.dir / "songarr.db"))
        self.sam = self.users.create("Sam")
        self.users.set_password(self.sam, "sam", "correct horse battery")
        self.running = self.most = 0
        self.count_lock = threading.Lock()
        real = users.check_password

        def slow_check(password, stored):  # a slower check, so the tries all overlap
            with self.count_lock:
                self.running += 1
                self.most = max(self.most, self.running)
            try:
                time.sleep(0.05)
                return real(password, stored)
            finally:
                with self.count_lock:
                    self.running -= 1
        patcher = mock.patch.object(users, "check_password", slow_check)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def all_at_once(self, n: int, attempt) -> Counter:
        start, results = threading.Barrier(n), Counter()

        def run(i):
            start.wait()
            try:
                attempt(i)
                outcome = "ok"
            except SignInError as e:
                outcome = e.status
            with self.count_lock:
                results[outcome] += 1
        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_guesses_sent_together_still_get_five_tries_per_name(self):
        got = self.all_at_once(30, lambda i: self.users.sign_in("sam", f"guess {i}", "x", f"9.9.{i}.1"))
        self.assertEqual(got, Counter({429: 30 - users.LOGIN_TRIES, 401: users.LOGIN_TRIES}))
        with self.assertRaises(SignInError) as e:  # the name waits now, even with the right password
            self.users.sign_in("sam", "correct horse battery", "x", "8.8.8.8")
        self.assertEqual(e.exception.status, 429)

    def test_guesses_sent_together_from_one_address(self):
        got = self.all_at_once(30, lambda i: self.users.sign_in(f"name{i}", "guess", "x", "6.6.6.6"))
        self.assertEqual(got, Counter({429: 20, 401: 10}))
        self.assertLessEqual(self.most, users.MAX_CHECKS)  # the ten checks took turns

    def test_right_passwords_dont_count(self):
        for _ in range(12):  # more than an address's ten wrong tries
            self.users.sign_in("sam", "correct horse battery", "Phone", "5.5.5.5")
        self.assertFalse(self.users._throttled("5.5.5.5"))
        with self.assertRaises(SignInError):
            self.users.sign_in("sam", "wrong", "Phone", "5.5.5.5")
        self.users.sign_in("sam", "correct horse battery", "Phone", "5.5.5.5")
        self.assertEqual(self.users._name_tries("sam"), 0)  # a right password starts the count again

    def test_changing_the_password_with_guesses_sent_together(self):
        got = self.all_at_once(20, lambda i: self.users.change_password(self.sam, "sam", f"guess {i}", "a new password", "device:1"))
        self.assertEqual(got, Counter({429: 20 - users.LOGIN_TRIES, 403: users.LOGIN_TRIES}))
        self.assertEqual(self.users.sign_in("sam", "correct horse battery", "x", "8.8.8.8")[1], self.sam)  # unchanged


class _Public(BaseHTTPRequestHandler):
    """The server on the internet: says which name it was asked for, and sends some on elsewhere."""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/hop/"):
            self.send_response(302)
            self.send_header("Location", f"http://{self.path[5:]}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = f"public {self.headers['Host']}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Private(BaseHTTPRequestHandler):
    """Something on the home network that must never be reached."""
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        body = b"private"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RebindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = [ThreadingHTTPServer(("127.0.0.1", 0), h) for h in (_Public, _Private)]
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.public_port, cls.private_port = (s.server_address[1] for s in cls.servers)

    @classmethod
    def tearDownClass(cls):
        for s in cls.servers:
            s.shutdown()
            s.server_close()

    def setUp(self):
        self.before, podcasts.allow_private_hosts = podcasts.allow_private_hosts, False
        _Private.hits = 0
        self.lookups: Counter = Counter()
        # what each made-up name answers, lookup by lookup (the last answer repeats)
        self.answers = {"rebind.example": [PUBLIC, "127.0.0.1"], "rebind2.example": [PUBLIC, "127.0.0.1"],
                        "inside.example": ["127.0.0.1"]}
        self.target = self.public_port  # where connections to PUBLIC go
        real_lookup, real_connect = socket.getaddrinfo, socket.create_connection

        def lookup(host, port, *args, **kwargs):
            if host not in self.answers:
                return real_lookup(host, port, *args, **kwargs)
            self.lookups[host] += 1
            given = self.answers[host]
            ip = given.pop(0) if len(given) > 1 else given[0]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

        def connect(address, *args, **kwargs):
            if address[0] == PUBLIC:
                return real_connect(("127.0.0.1", self.target), *args, **kwargs)
            return real_connect(address, *args, **kwargs)
        for patcher in (mock.patch.object(socket, "getaddrinfo", lookup), mock.patch.object(socket, "create_connection", connect)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        podcasts.allow_private_hosts = self.before

    def test_a_second_answer_cant_point_inside(self):
        with podcasts.open_url(f"http://rebind.example:{self.private_port}/episode.mp3", timeout=5) as r:
            self.assertEqual(r.read(), f"public rebind.example:{self.private_port}".encode())  # the Host header keeps the name
        self.assertEqual((self.lookups["rebind.example"], _Private.hits), (1, 0))

    def test_names_pointing_inside_are_refused(self):
        with self.assertRaises(podcasts.UnsafeURL):
            podcasts.open_url(f"http://inside.example:{self.private_port}/", timeout=5)
        self.assertEqual(_Private.hits, 0)

    def test_redirects_inside_are_refused(self):
        for name in ("inside.example", "rebind2.example"):  # pointing inside at once, or on the second look
            try:
                with podcasts.open_url(f"http://rebind.example:{self.private_port}/hop/{name}:{self.private_port}/", timeout=5) as r:
                    self.assertTrue(r.read().startswith(b"public"))
            except podcasts.UnsafeURL:
                pass
            self.answers["rebind.example"] = [PUBLIC, "127.0.0.1"]
        self.assertEqual(_Private.hits, 0)

    def test_https_names_the_host_to_the_server(self):
        hello = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            self.target = listener.getsockname()[1]

            def accept():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(5)
                    hello.append(conn.recv(4096))  # the TLS hello: the name it's checking the certificate against
            t = threading.Thread(target=accept)
            t.start()
            with self.assertRaises(OSError):
                podcasts.open_url(f"https://rebind.example:{self.private_port}/", timeout=5)
            t.join()
        self.assertIn(b"rebind.example", b"".join(hello))
        self.assertEqual(_Private.hits, 0)

    def test_proxies_are_never_used(self):  # a proxy would look the name up itself, unchecked
        code = ("import os, urllib.request\n"
                "os.environ['HTTP_PROXY'] = os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9'\n"
                "from songarr import podcasts\n"
                "print([h.proxies for h in podcasts._opener.handlers if isinstance(h, urllib.request.ProxyHandler)])")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "[]")  # (none at all: the environment's would have been used)


if __name__ == "__main__":
    unittest.main()
