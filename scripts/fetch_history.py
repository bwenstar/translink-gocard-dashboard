#!/usr/bin/env python3
"""Download go card travel history CSVs into data/raw/.

go card online only exports up to ~6 months per request (longer ranges come back
empty), so this splits the period into 180-day windows and saves each one to
data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv. build.py merges and de-dupes them.

Modes:
  (default)      incremental: from a week before the newest trip already in data/raw/ to today
  --all          walk back from today until two windows in a row have no trips
  --from/--to    an explicit range (DD/MM/YYYY or YYYY-MM-DD)

Credentials: GOCARD_CARD / GOCARD_PASSWORD from the environment, else USERNAME /
PASSWORD (or GOCARD_CARD / GOCARD_PASSWORD) from .env. USERNAME is deliberately not
read from the environment, where it is usually your OS login name.
"""
import argparse
import csv
import datetime as dt
import os
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://gocard.translink.com.au/webtix"
LOGIN_URL = f"{BASE}/welcome/welcome.do"
EXPORT_URL = f"{BASE}/cardinfo/history.do"
WINDOW_DAYS = 180
OVERLAP_DAYS = 7
DELAY_S = 2.0
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


def credentials(env_path):
    env = read_env(env_path)
    card = os.environ.get("GOCARD_CARD") or env.get("GOCARD_CARD") or env.get("USERNAME")
    pw = os.environ.get("GOCARD_PASSWORD") or env.get("GOCARD_PASSWORD") or env.get("PASSWORD")
    if not card or not pw:
        sys.exit(f"No credentials: set USERNAME and PASSWORD in {env_path} (or GOCARD_CARD / GOCARD_PASSWORD)")
    card = card.replace(" ", "")
    if not (card.isdigit() and len(card) == 16):
        sys.exit("The go card number (USERNAME) should be 16 digits")
    return card, pw


def login(card, pw):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.get(LOGIN_URL, timeout=30).raise_for_status()
    r = s.post(LOGIN_URL, data={"cardOps": "Display", "cardNum": card, "pass": pw},
               headers={"Referer": LOGIN_URL}, timeout=30)
    r.raise_for_status()
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


def latest_trip_date(raw_dir):
    latest = None
    for path in raw_dir.rglob("*.csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                try:
                    d = dt.datetime.strptime(row[0].strip(), "%d/%b/%Y").date()
                except (ValueError, IndexError):
                    continue
                latest = d if latest is None or d > latest else latest
    return latest


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="fetch the whole history")
    mode.add_argument("--from", dest="start", type=parse_date, help="start of an explicit range")
    ap.add_argument("--to", dest="end", type=parse_date, help="end of the range (default: today)")
    ap.add_argument("--raw", type=pathlib.Path, default=ROOT / "data" / "raw", help="where to save exports")
    ap.add_argument("--env", type=pathlib.Path, default=ROOT / ".env", help="credentials file")
    args = ap.parse_args()

    today = dt.date.today()
    end = args.end or today
    if args.all:
        start = None
    elif args.start:
        start = args.start
    else:
        latest = latest_trip_date(args.raw)
        if latest is None:
            sys.exit(f"No existing exports in {args.raw} - run with --all for the first download")
        start = latest - dt.timedelta(days=OVERLAP_DAYS)

    card, pw = credentials(args.env)
    s = login(card, pw)
    print(f"Logged in as card ...{card[-4:]}")
    saved, total, empty_run = [], 0, 0
    try:
        for w0, w1 in windows_back(start or dt.date(2000, 1, 1), end):
            res = export(s, w0, w1)
            if res is None:
                sys.exit(f"The site rejected {w0}..{w1} (range too long?)")
            text, n = res
            print(f"  {w0:%d %b %Y} - {w1:%d %b %Y}: {n} rows")
            if n:
                saved.append(save(text, w0, w1, args.raw))
                total += n
                empty_run = 0
            else:
                empty_run += 1
                if start is None and empty_run >= 2:
                    break
            time.sleep(DELAY_S)
    finally:
        logout(s)
    print(f"Saved {total} rows in {len(saved)} file(s) under {args.raw}. Next: python3 scripts/build.py")


if __name__ == "__main__":
    main()
