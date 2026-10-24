"""serve.py: password protection, refusing to expose data without one, no listings or escapes."""
import base64
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import fakes  # noqa: F401  (puts scripts/ on the path)
import serve


class Server(unittest.TestCase):
    def start(self, **kw):
        self.root = pathlib.Path(tempfile.mkdtemp())
        site = self.root / "dashboard"
        site.mkdir()
        (site / "index.html").write_text("<h1>dash</h1>")
        (site / "data.js").write_text("window.TRAVEL = {};")
        (self.root / ".env").write_text("PASSWORD=go-card-secret\n")  # outside the served folder
        self.server = serve.make_server("127.0.0.1", 0, site, **kw)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path, user=None, password=None):
        req = urllib.request.Request(self.base + path)
        if user is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, "", dict(e.headers)

    def test_password_required(self):
        self.start(user="gocard", password="pw")
        status, _, headers = self.get("/data.js")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.get("/data.js", "gocard", "wrong")[0], 401)
        self.assertEqual(self.get("/data.js", "someone", "pw")[0], 401)
        status, body, headers = self.get("/data.js", "gocard", "pw")
        self.assertEqual((status, body), (200, "window.TRAVEL = {};"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("noindex", headers["X-Robots-Tag"])

    def test_no_password_on_localhost(self):
        self.start()
        self.assertEqual(self.get("/")[0], 200)

    def test_no_listing_and_no_escape(self):
        self.start(user="gocard", password="pw")
        (self.root / "dashboard" / "sub").mkdir()
        self.assertEqual(self.get("/sub/", "gocard", "pw")[0], 404)          # no directory listings
        for path in ("/../.env", "/%2e%2e/.env", "/..%2f.env"):
            status, body, _ = self.get(path, "gocard", "pw")
            self.assertNotIn("go-card-secret", body, path)               # nothing outside dashboard/


class Startup(unittest.TestCase):
    def test_refuses_network_without_password(self):
        env = pathlib.Path(tempfile.mkdtemp()) / ".env"
        env.write_text("USERNAME=1111222233334444\n")
        with self.assertRaises(SystemExit) as e:
            serve.main(["--host", "0.0.0.0", "--port", "0", "--env", str(env)])
        self.assertIn("without a password", str(e.exception))

    def test_loopback_detection(self):
        for host, local in [("127.0.0.1", True), ("::1", True), ("localhost", True),
                            ("0.0.0.0", False), ("192.168.1.20", False), ("100.64.0.1", False)]:
            self.assertEqual(serve.is_loopback(host), local, host)


if __name__ == "__main__":
    unittest.main()
