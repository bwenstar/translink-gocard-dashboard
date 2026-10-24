#!/usr/bin/env python3
"""Download go card travel history and merge it into the master file data/history.csv.

go card online only exports up to ~6 months per request (longer ranges come back
with no rows), so any range is split into 180-day windows. Each window is merged into the
master (overlaps are de-duplicated) and a copy of the untouched export is kept in
data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv as a backup.

Modes:
  (default)      incremental: from a week before the newest trip in the master to today
  --all          walk back from today until two windows in a row have no trips
  --years N      the last N years, e.g. --years 6 (any length; split into windows)
  --from/--to    an explicit range of any length (DD/MM/YYYY or YYYY-MM-DD)
  --import PATH  merge CSVs you exported by hand (files or folders) without logging in
  --check-site   check the live site still works the way docs/site-contract.json records,
                 and save a private snapshot of its pages to data/site-snapshots/

Every run checks the login form before sending credentials and the CSV columns of every
download against docs/site-contract.json, and stops with a clear message if Translink
has changed something.

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
import json
import os
import pathlib
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

import cards
from cards import read_env  # noqa: F401  (re-exported: serve.py imports it from here)
from history import Master, parse_rows, read_export

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://gocard.translink.com.au/webtix"
LOGIN_URL = f"{BASE}/welcome/welcome.do"
EXPORT_URL = f"{BASE}/cardinfo/history.do"
WINDOW_DAYS = 180
OVERLAP_DAYS = 7
DELAY_S = 2.0
HOST = "gocard.translink.com.au"
CONTRACT = json.loads((ROOT / "docs" / "site-contract.json").read_text())
SITE_CHANGED = ("Translink's site has changed since docs/site-contract.json was written ({}). "
                "Run: python3 scripts/fetch_history.py --check-site, and see README 'If the site changes'.")
EXAMPLE_CARD = "0123456789012345"  # the placeholder in .env.example
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def warn_if_readable(path):
    """.env holds a password: warn if other users on this machine can read it."""
    if os.name == "posix" and path.exists() and path.stat().st_mode & 0o077:
        print(f"Warning: {path} is readable by other users. Fix with: chmod 600 {path}", file=sys.stderr)


def credentials(env_path, ask=False, n=1):
    """(card number, password) for card n (see cards.py): USERNAME/PASSWORD, or USERNAME_2/... ."""
    warn_if_readable(env_path)
    env = read_env(env_path)
    sfx = cards.suffix(n)
    card = os.environ.get(f"GOCARD_CARD{sfx}") or env.get(f"GOCARD_CARD{sfx}") or env.get(f"USERNAME{sfx}")
    pw = None if ask else (os.environ.get(f"GOCARD_PASSWORD{sfx}") or env.get(f"GOCARD_PASSWORD{sfx}")
                           or env.get(f"PASSWORD{sfx}"))
    if not card:
        sys.exit(f"No go card number: set USERNAME{sfx} in {env_path} (copy .env.example) or GOCARD_CARD{sfx}")
    card = card.replace(" ", "")
    if card == EXAMPLE_CARD:
        sys.exit(f"{env_path} still has the example card number - fill in your own")
    if not (card.isdigit() and len(card) == 16):
        sys.exit(f"The go card number (USERNAME{sfx}) should be 16 digits")
    if not pw:
        if not sys.stdin.isatty():
            sys.exit(f"No password: set PASSWORD{sfx} in .env or GOCARD_PASSWORD{sfx}, or run interactively to be asked")
        pw = getpass.getpass(f"go card online password for card ...{card[-4:]}: ")
    return card, pw


class _Forms(HTMLParser):
    """Collects each <form>'s attributes and its named inputs."""
    def __init__(self):
        super().__init__()
        self.forms, self._cur = {}, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"method": (a.get("method") or "get").lower(), "action": a.get("action"), "fields": {}}
            self.forms[a.get("id") or a.get("name") or f"form{len(self.forms)}"] = self._cur
        elif tag in ("input", "select", "textarea") and self._cur is not None and a.get("name"):
            f = {"type": (a.get("type") or tag).lower()}
            if f["type"] == "hidden":
                f["value"] = a.get("value")
            self._cur["fields"][a["name"]] = f

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def login_form_problems(html):
    """Differences between the live login form and the contract. Empty list = unchanged."""
    want = CONTRACT["login"]
    p = _Forms()
    p.feed(html)
    form = p.forms.get(want["form_id"])
    if form is None:
        return [f'no form with id "{want["form_id"]}" on the login page']
    out = []
    if form["method"] != want["method"]:
        out.append(f'login form method is {form["method"]}, expected {want["method"]}')
    if form["action"] != want["action"]:
        out.append(f'login form posts to {form["action"]}, expected {want["action"]}')
    for name, spec in want["fields"].items():
        got = form["fields"].get(name)
        if got is None:
            out.append(f'login form has no "{name}" field')
        elif got != spec:
            out.append(f'login field "{name}" is {got}, expected {spec}')
    extra = set(form["fields"]) - set(want["fields"])
    if extra:
        out.append(f"login form has new fields: {', '.join(sorted(extra))} (a captcha or token?)")
    return out


def header_problem(text):
    """None if the CSV's header matches the contract, else a description."""
    first = next((l for l in text.splitlines() if l.strip()), "")
    got = [c.strip() for c in first.split(",")]
    want = CONTRACT["export"]["csv_header"]
    return None if got == want else f"CSV columns are {got}, expected {want}"


def login(card, pw, snapshot=None):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    page = s.get(LOGIN_URL, timeout=30)
    page.raise_for_status()
    if snapshot:
        snapshot("login.html", page.text)
    problems = login_form_problems(page.text)
    if problems:  # never type credentials into a form we don't recognise
        sys.exit(SITE_CHANGED.format("; ".join(problems)))
    r = s.post(LOGIN_URL, data={"cardOps": "Display", "cardNum": card, "pass": pw},
               headers={"Referer": LOGIN_URL}, timeout=30)
    r.raise_for_status()
    if urlparse(r.url).hostname != HOST:
        sys.exit(f"Login redirected to {urlparse(r.url).hostname} - stopping (expected {HOST})")
    if 'id="login-form"' in r.text or "/online/summary" not in r.url:
        sys.exit("Login failed - check the card number and password in .env "
                 "(stopping now to avoid locking the account)")
    if snapshot:
        snapshot("after-login.html", r.text)
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
        sys.exit(SITE_CHANGED.format("the export returned a web page instead of CSV"))
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    problem = header_problem(text)
    if problem:
        sys.exit(SITE_CHANGED.format(problem))
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


def check_site(args):
    """Log in, fetch the history page and a one-week export, compare all of it with the contract,
    and keep the raw pages in a private, gitignored snapshot folder for whoever fixes it later."""
    folder = args.raw.parent / "site-snapshots" / dt.date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)

    def snapshot(name, text):
        (folder / name).write_text(text, encoding="utf-8")

    card, pw = credentials(args.env, args.ask, args.card)
    print(f"Checking {BASE} against docs/site-contract.json (captured {CONTRACT['captured']})")
    s = login(card, pw, snapshot)  # exits with the differences if the login form changed
    print("  login form and login: OK")
    try:
        time.sleep(DELAY_S)
        page = s.get(EXPORT_URL, timeout=60)
        snapshot("history.html", page.text)
        if "csvExport" not in page.text:
            print("  history page: no CSV export link found - the export may have moved")
        else:
            print("  history page: export link present")
        time.sleep(DELAY_S)
        end = dt.date.today()
        res = export(s, end - dt.timedelta(days=6), end)  # exits if the columns changed
        print(f"  CSV export: OK ({res[1] if res else 0} rows in the last week, columns as expected)")
        time.sleep(DELAY_S)
        too_long = export(s, end - dt.timedelta(days=400), end)
        cutoff = end - dt.timedelta(days=WINDOW_DAYS)
        old = [r for r in parse_rows(too_long[0].splitlines())
               if dt.datetime.strptime(r[0], "%d/%b/%Y").date() < cutoff] if too_long else []
        if too_long is None:
            print("  6-month limit: unchanged (a 400-day range comes back empty)")
        elif old:
            print(f"  6-month limit: a 400-day range returned {len(old)} rows older than {WINDOW_DAYS} days "
                  f"- the limit may have been lifted (WINDOW_DAYS could be raised)")
        elif too_long[1] == 0:
            print("  6-month limit: unchanged (a 400-day range returns only the CSV header, no rows)")
        else:
            print(f"  6-month limit: a 400-day range returned {too_long[1]} rows but none older than "
                  f"{WINDOW_DAYS} days - still limited, just answered differently (180-day windows still work)")
    finally:
        logout(s)
    (folder / "README.txt").write_text(
        "Raw pages from go card online, saved by fetch_history.py --check-site.\n"
        "They include your card details and balance: keep them private (data/ is gitignored).\n")
    print(f"All checks passed. Raw pages saved privately to {folder}")


def import_files(paths, master):
    files = [f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob("*.csv")))]
    if not files:
        sys.exit("No CSV files found in " + ", ".join(map(str, paths)))
    for f in files:
        added, updated = master.merge(read_export(f))
        print(f"  {f}: {added} new, {updated} updated")


def main():
    try:
        run()
    except requests.RequestException as e:
        sys.exit(f"Couldn't reach go card online ({type(e).__name__}). The site may be down or "
                 f"rate-limiting; try again later. Anything downloaded before the error was kept.")


def run():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="fetch the whole history")
    mode.add_argument("--years", type=float, help="fetch the last N years")
    mode.add_argument("--from", dest="start", type=parse_date, help="start of an explicit range")
    mode.add_argument("--check-site", action="store_true",
                      help="check the site against docs/site-contract.json and save a private snapshot")
    mode.add_argument("--import", dest="imports", type=pathlib.Path, nargs="+", metavar="PATH",
                      help="merge local CSV exports (files or folders) into the master, no download")
    ap.add_argument("--to", dest="end", type=parse_date, help="end of the range (default: today)")
    ap.add_argument("--card", type=int, default=1,
                    help="which card (see scripts/cards.py): 1 = USERNAME/PASSWORD, 2 = USERNAME_2/PASSWORD_2, ...")
    ap.add_argument("--history", type=pathlib.Path,
                    help="master history file (default: data/history.csv, or data/history.cardN.csv)")
    ap.add_argument("--raw", type=pathlib.Path,
                    help="where to keep a copy of each download (default: data/raw, or data/raw-cardN)")
    ap.add_argument("--no-raw", action="store_true", help="don't keep per-window copies")
    ap.add_argument("--env", type=pathlib.Path, default=ROOT / ".env", help="credentials file")
    ap.add_argument("--ask", action="store_true", help="prompt for the password instead of reading it")
    args = ap.parse_args()
    default_history, default_raw = cards.paths(args.card, ROOT / "data")
    args.history = args.history or default_history
    args.raw = args.raw or default_raw

    if args.check_site:
        return check_site(args)
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

    card, pw = credentials(args.env, args.ask, args.card)
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
