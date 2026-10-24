#!/usr/bin/env python3
"""Download go card travel history and merge it into the master file data/history.csv.

go card online only exports up to ~6 months per request (longer ranges come back
empty), so any range is split into 180-day windows. Each window is merged into the
master (overlaps are de-duplicated) and a copy of the untouched export is kept in
data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv as a backup.

Modes:
  (default)      incremental: from a week before the newest trip in the master to today
  --all          walk back from today until two windows in a row have no trips
  --years N      the last N years, e.g. --years 6 (any length; split into windows)
  --from/--to    an explicit range of any length (DD/MM/YYYY or YYYY-MM-DD)
  --import PATH  merge CSVs you exported by hand (files or folders) without logging in

Credentials: GOCARD_CARD / GOCARD_PASSWORD from the environment, else USERNAME /
PASSWORD (or GOCARD_CARD / GOCARD_PASSWORD) from .env. USERNAME is deliberately not
read from the environment, where it is usually your OS login name. Leave PASSWORD out
(or pass --ask) to be prompted for it instead of storing it.

Your card number and password are only ever sent to LOGIN_URL below, over HTTPS. They
are never printed, logged, saved anywhere else, or sent to any other site.
"""
import argparse
import datetime as dt
import getpass
import os
import pathlib
import sys
import time
from urllib.parse import urlparse

import requests

from history import Master, parse_rows, read_export

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://gocard.translink.com.au/webtix"
LOGIN_URL = f"{BASE}/welcome/welcome.do"
EXPORT_URL = f"{BASE}/cardinfo/history.do"
WINDOW_DAYS = 180
OVERLAP_DAYS = 7
DELAY_S = 2.0
HOST = "gocard.translink.com.au"
EXAMPLE_CARD = "0123456789012345"  # the placeholder in .env.example
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def read_env(path):
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return vals


def warn_if_readable(path):
    """.env holds a password: warn if other users on this machine can read it."""
    if os.name == "posix" and path.exists() and path.stat().st_mode & 0o077:
        print(f"Warning: {path} is readable by other users. Fix with: chmod 600 {path}", file=sys.stderr)


def credentials(env_path, ask=False):
    warn_if_readable(env_path)
    env = read_env(env_path)
    card = os.environ.get("GOCARD_CARD") or env.get("GOCARD_CARD") or env.get("USERNAME")
    pw = None if ask else os.environ.get("GOCARD_PASSWORD") or env.get("GOCARD_PASSWORD") or env.get("PASSWORD")
    if not card:
        sys.exit(f"No go card number: set USERNAME in {env_path} (copy .env.example) or GOCARD_CARD")
    card = card.replace(" ", "")
    if card == EXAMPLE_CARD:
        sys.exit(f"{env_path} still has the example card number - fill in your own")
    if not (card.isdigit() and len(card) == 16):
        sys.exit("The go card number (USERNAME) should be 16 digits")
    if not pw:
        if not sys.stdin.isatty():
            sys.exit("No password: set PASSWORD in .env or GOCARD_PASSWORD, or run interactively to be asked")
        pw = getpass.getpass(f"go card online password for card ...{card[-4:]}: ")
    return card, pw


def login(card, pw):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.get(LOGIN_URL, timeout=30).raise_for_status()
    r = s.post(LOGIN_URL, data={"cardOps": "Display", "cardNum": card, "pass": pw},
               headers={"Referer": LOGIN_URL}, timeout=30)
    r.raise_for_status()
    if urlparse(r.url).hostname != HOST:
        sys.exit(f"Login redirected to {urlparse(r.url).hostname} - stopping (expected {HOST})")
    if 'id="login-form"' in r.text or "/online/summary" not in r.url:
        sys.exit("Login failed - check the card number and password in .env "
                 "(stopping now to avoid locking the account)")
    return s


def logout(s):
    try:
        s.get(LOGIN_URL, params={"logout": "true"}, timeout=30)
    except requests.RequestException:
        pass


def export(s, start, end):
    """CSV rows for start..end (inclusive), header excluded. None if the site rejected the range."""
    r = s.get(EXPORT_URL, timeout=60, params={
        "csvExport": "yes", "startDate": start.strftime("%d/%m/%Y"), "endDate": end.strftime("%d/%m/%Y")})
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    if "<html" in text[:500].lower():
        sys.exit("Got an HTML page instead of CSV - the session may have expired or the site changed")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    return text, len(lines) - 1


def save(text, start, end, raw_dir):
    folder = raw_dir / f"{start:%y%m%d}-{end:%y%m%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Travel_history.csv"
    path.write_text(text, encoding="utf-8")
    return path


def parse_date(s):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"bad date {s!r} (use DD/MM/YYYY or YYYY-MM-DD)")


def windows_back(start, end):
    """180-day windows from end back to start, newest first."""
    while end >= start:
        w0 = max(start, end - dt.timedelta(days=WINDOW_DAYS - 1))
        yield w0, end
        end = w0 - dt.timedelta(days=1)


def import_files(paths, master):
    files = [f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob("*.csv")))]
    if not files:
        sys.exit("No CSV files found in " + ", ".join(map(str, paths)))
    for f in files:
        added, updated = master.merge(read_export(f))
        print(f"  {f}: {added} new, {updated} updated")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="fetch the whole history")
    mode.add_argument("--years", type=float, help="fetch the last N years")
    mode.add_argument("--from", dest="start", type=parse_date, help="start of an explicit range")
    mode.add_argument("--import", dest="imports", type=pathlib.Path, nargs="+", metavar="PATH",
                      help="merge local CSV exports (files or folders) into the master, no download")
    ap.add_argument("--to", dest="end", type=parse_date, help="end of the range (default: today)")
    ap.add_argument("--history", type=pathlib.Path, default=ROOT / "data" / "history.csv",
                    help="master history file (default: data/history.csv)")
    ap.add_argument("--raw", type=pathlib.Path, default=ROOT / "data" / "raw",
                    help="where to keep a copy of each download (default: data/raw)")
    ap.add_argument("--no-raw", action="store_true", help="don't keep per-window copies")
    ap.add_argument("--env", type=pathlib.Path, default=ROOT / ".env", help="credentials file")
    ap.add_argument("--ask", action="store_true", help="prompt for the password instead of reading it")
    args = ap.parse_args()

    master = Master(args.history)
    before = len(master.rows)
    if args.imports:
        import_files(args.imports, master)
        print(f"{master.save()} rows in {args.history} ({len(master.rows) - before} new)")
        return

    today = dt.date.today()
    end = args.end or today
    if args.all:
        start = None
    elif args.years:
        start = end - dt.timedelta(days=round(args.years * 365.25))
    elif args.start:
        start = args.start
    else:
        latest = master.latest_date()
        if latest is None:
            sys.exit(f"{args.history} is empty - run with --all (or --years N) for the first download, "
                     f"or --import data/raw to build it from exports you already have")
        start = latest - dt.timedelta(days=OVERLAP_DAYS)
    if start and start > end:
        sys.exit("--from is after --to")

    card, pw = credentials(args.env, args.ask)
    s = login(card, pw)
    print(f"Logged in as card ...{card[-4:]}")
    total = added = updated = empty_run = 0
    try:
        for w0, w1 in windows_back(start or dt.date(2000, 1, 1), end):
            res = export(s, w0, w1)
            if res is None:
                sys.exit(f"The site rejected {w0}..{w1} (range too long?)")
            text, n = res
            if n:
                if not args.no_raw:
                    save(text, w0, w1, args.raw)
                a, u = master.merge(parse_rows(text.splitlines()))
                master.save()  # after every window, so an interrupted run keeps what it got
                added, updated, total = added + a, updated + u, total + n
                empty_run = 0
            else:
                empty_run += 1
                a = u = 0
            print(f"  {w0:%d %b %Y} - {w1:%d %b %Y}: {n} rows, {a} new, {u} updated")
            if start is None and empty_run >= 2:
                break
            time.sleep(DELAY_S)
    finally:
        logout(s)
    print(f"Downloaded {total} rows: {added} new, {updated} updated. "
          f"{len(master.rows)} rows in {args.history}. Next: python3 scripts/build.py")


if __name__ == "__main__":
    main()
