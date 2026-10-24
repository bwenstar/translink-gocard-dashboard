#!/usr/bin/env python3
"""Serve the dashboard (dashboard/) over HTTP, password-protected when it's on the network.

  python3 scripts/serve.py                         # http://127.0.0.1:8000, this computer only
  python3 scripts/serve.py --host 0.0.0.0 --port 8765    # the whole network: needs a password

The dashboard holds your travel history, so serving it beyond this computer requires a
password. Set DASHBOARD_PASSWORD (and optionally DASHBOARD_USER, default "gocard") in .env
or the environment, and the browser asks for them (HTTP Basic auth). Without one, it refuses
to listen on anything but localhost, unless you pass --no-auth.

Basic auth over plain HTTP is fine on your own home network or Tailscale. Don't expose this
port to the internet (port forwarding, Tailscale Funnel).
"""
import argparse
import base64
import functools
import hmac
import http.server
import ipaddress
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from fetch_history import read_env  # noqa: E402


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class Handler(http.server.SimpleHTTPRequestHandler):
    auth = None  # expected "Basic ..." header value, or None for no auth

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")          # always the latest data.js
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def authorised(self):
        if self.auth is None:
            return True
        if hmac.compare_digest(self.headers.get("Authorization", ""), self.auth):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="go card dashboard", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if self.authorised():
            super().do_GET()

    def do_HEAD(self):
        if self.authorised():
            super().do_HEAD()

    def list_directory(self, path):  # never list files, just serve the dashboard's own
        self.send_error(404)
        return None


def make_server(host, port, directory, user=None, password=None):
    handler = functools.partial(Handler, directory=str(directory))
    Handler.auth = None
    if password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        Handler.auth = f"Basic {token}"
    return http.server.ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; 0.0.0.0 = all)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", type=pathlib.Path, default=ROOT / "dashboard")
    ap.add_argument("--env", type=pathlib.Path, default=ROOT / ".env")
    ap.add_argument("--no-auth", action="store_true", help="allow serving on the network without a password")
    args = ap.parse_args(argv)

    env = read_env(args.env)
    password = os.environ.get("DASHBOARD_PASSWORD") or env.get("DASHBOARD_PASSWORD")
    user = os.environ.get("DASHBOARD_USER") or env.get("DASHBOARD_USER") or "gocard"
    if not password and not is_loopback(args.host) and not args.no_auth:
        sys.exit(f"Refusing to serve your travel data on {args.host} without a password. "
                 f"Set DASHBOARD_PASSWORD in {args.env}, or use --host 127.0.0.1.")
    if not (args.dir / "data.js").exists():
        print(f"Note: no {args.dir / 'data.js'} yet - run scripts/build.py (or make update).", file=sys.stderr)

    server = make_server(args.host, args.port, args.dir, user, password)
    shown = "localhost" if args.host in ("127.0.0.1", "::1") else args.host
    print(f"Serving {args.dir} at http://{shown}:{args.port}/"
          + (f" (user {user!r}, password from DASHBOARD_PASSWORD)" if password else " (no password)"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
