#!/usr/bin/env python3
"""Bring everything up to date in one go. This is what `make update` and the weekly timer run.

  1. refresh Translink's timetable (fetch_gtfs.py) if it's older than --gtfs-days
  2. download new go card trips (fetch_history.py). On a first run it imports data/raw/
     if you have exports there, otherwise it downloads the whole history (--all).
  3. rebuild the dashboard (build.py)

Every run appends to data/update.log and writes data/last-update.json. On failure it
exits non-zero and sends a desktop notification (notify-send on Linux, osascript on
macOS), plus --notify-cmd if you give one, e.g. to get a phone alert:

  python3 scripts/update.py --notify-cmd 'curl -s -d "$GOCARD_MESSAGE" ntfy.sh/your-topic'

The command gets GOCARD_STATUS (ok/failed), GOCARD_KIND (login, site-changed, network,
error) and GOCARD_MESSAGE in its environment.

Login safety: after a failed login, later runs (the timer, say) don't try again until
.env has been changed or you pass --force. A wrong password is never retried week after
week, so it can't lock your card.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def run_step(name, argv, log):
    """Run one script; echo and log its output. Returns (exit code, stdout, stderr)."""
    argv = [str(a) for a in argv]
    log.write(f"$ {' '.join(argv)}\n")
    p = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True)
    for stream, text in ((sys.stdout, p.stdout), (sys.stderr, p.stderr)):
        if text:
            stream.write(text)
            log.write(text)
    return p.returncode, p.stdout, p.stderr


def classify(stderr):
    """What kind of failure fetch_history.py reported (its exit messages are stable)."""
    if "Login failed" in stderr or "No go card number" in stderr or "No password" in stderr \
            or "example card number" in stderr or "should be 16 digits" in stderr:
        return "login"
    if "site has changed" in stderr or "redirected to" in stderr:
        return "site-changed"
    if "Couldn't reach" in stderr:
        return "network"
    return "error"


ADVICE = {
    "login": "Check USERNAME/PASSWORD in .env. Automatic runs are paused until .env changes "
             "(or run: python3 scripts/update.py --force).",
    "site-changed": "Translink's site no longer matches docs/site-contract.json. "
                    "Run: python3 scripts/fetch_history.py --check-site",
    "network": "Couldn't reach go card online; the next run will try again.",
    "error": "See data/update.log.",
}


def notify(title, message, args, status, kind):
    if args.no_desktop:
        pass
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=go card", title, message], check=False)
    elif sys.platform == "darwin" and shutil.which("osascript"):
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
                       check=False)
    if args.notify_cmd:
        env = dict(os.environ, GOCARD_STATUS=status, GOCARD_KIND=kind or "", GOCARD_MESSAGE=f"{title}: {message}")
        subprocess.run(args.notify_cmd, shell=True, env=env, check=False)


@contextlib.contextmanager
def single_instance(path):
    """Yields False if another update is already running."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows: no flock; the timer there is your own Task Scheduler entry
            pass
        except OSError:
            yield False
            return
        yield True


def has_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip()) > 1
    except OSError:
        return False


def fetch_args(args):
    """The fetch_history.py call for this run: incremental, or the right first-run mode."""
    common = ["--history", args.history, "--raw", args.raw, "--env", args.env]
    if has_rows(args.history):
        return [SCRIPTS / "fetch_history.py", *common], None
    if any(args.raw.rglob("*.csv")) if args.raw.exists() else False:
        return [SCRIPTS / "fetch_history.py", *common, "--import", args.raw], "importing data/raw/ (first run)"
    return [SCRIPTS / "fetch_history.py", *common, "--all"], "downloading the whole history (first run)"


def main(argv=None):
    try:
        return _main(argv)
    except Exception as e:  # a crash in an unattended run must still be reported, not just logged by systemd
        notify("go card update crashed", f"{type(e).__name__}: {e}. See data/update.log.",
               argparse.Namespace(no_desktop=False, notify_cmd=os.environ.get("GOCARD_NOTIFY_CMD")), "failed", "error")
        raise


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs-days", type=float, default=30, help="refresh the timetable when older than this (default 30)")
    ap.add_argument("--no-fetch", action="store_true", help="skip downloading; just refresh and rebuild")
    ap.add_argument("--force", action="store_true", help="try to log in even after a failed login")
    ap.add_argument("--notify-cmd", default=os.environ.get("GOCARD_NOTIFY_CMD"),
                    help="shell command to run after each update (default: $GOCARD_NOTIFY_CMD)")
    ap.add_argument("--notify-success", action="store_true", help="also notify when an update succeeds")
    ap.add_argument("--no-desktop", action="store_true", help="no desktop notifications")
    ap.add_argument("--history", type=pathlib.Path, default=ROOT / "data" / "history.csv")
    ap.add_argument("--raw", type=pathlib.Path, default=ROOT / "data" / "raw")
    ap.add_argument("--env", type=pathlib.Path, default=ROOT / ".env")
    ap.add_argument("--gtfs-zip", type=pathlib.Path, default=ROOT / "data" / "gtfs" / "SEQ_GTFS.zip")
    ap.add_argument("--state", type=pathlib.Path, default=ROOT / "data", help="where the log and status files go")
    args = ap.parse_args(argv)

    state = args.state
    marker, status_file = state / ".login-failed", state / "last-update.json"
    state.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now().isoformat(timespec="seconds")
    result = {"started": started, "ok": False, "step": None, "kind": None, "message": "", "new_rows": None}

    with single_instance(state / ".update.lock") as mine, open(state / "update.log", "a", encoding="utf-8") as log:
        if not mine:
            print("Another update is already running; nothing to do.")
            return 0
        log.write(f"\n=== {started} ===\n")

        def fail(step, kind, message):
            result.update(step=step, kind=kind, message=message)
            print(f"\nUpdate failed at {step}: {message}", file=sys.stderr)
            notify("go card update failed", message, args, "failed", kind)
            return 1

        def finish(code):
            result["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            status_file.write_text(json.dumps(result, indent=2) + "\n")
            log.write(f"=== {'ok' if result['ok'] else 'FAILED'}: {result['message']}\n")
            return code

        # 1. timetable
        zip_age = (time.time() - args.gtfs_zip.stat().st_mtime) / 86400 if args.gtfs_zip.exists() else None
        if zip_age is None or zip_age > args.gtfs_days:
            print(f"Refreshing Translink's timetable ({'missing' if zip_age is None else f'{zip_age:.0f} days old'})...")
            code, _, err = run_step("gtfs", [SCRIPTS / "fetch_gtfs.py"], log)
            if code and not args.gtfs_zip.exists():
                return finish(fail("gtfs", "network", "Couldn't download Translink's timetable (needed once)."))
            if code:
                print("  Couldn't refresh the timetable; carrying on with the old one.")

        # 2. go card history
        if not args.no_fetch:
            env_changed = args.env.exists() and marker.exists() and args.env.stat().st_mtime > marker.stat().st_mtime
            if marker.exists() and not (args.force or env_changed):
                return finish(fail("fetch", "login", "Skipped: the last login failed. " + ADVICE["login"]))
            cmd, note = fetch_args(args)
            if note:
                print(f"Go card history: {note}")
            code, out, err = run_step("fetch", cmd, log)
            if code:
                kind = classify(err)
                if kind == "login":
                    marker.write_text(f"{started}\n{err.strip()}\n")
                last = (err.strip().splitlines() or ["unknown error"])[-1].rstrip()
                last += "" if last.endswith((".", "!", "?", ")")) else "."
                return finish(fail("fetch", kind, f"{last} {ADVICE[kind]}"))
            marker.unlink(missing_ok=True)
            m = re.search(r"(\d+) new", out)
            result["new_rows"] = int(m.group(1)) if m else None

        # 3. dashboard
        code, out, err = run_step("build", [SCRIPTS / "build.py", "--history", args.history], log)
        if code:
            last = (err.strip().splitlines() or ["unknown error"])[-1]
            return finish(fail("build", "error", last))

        n = result["new_rows"]
        result.update(ok=True, step="done",
                      message="Dashboard rebuilt" + ("" if n is None else f", {n} new row{'s' if n != 1 else ''}"))
        print(f"\n{result['message']}. Open dashboard/index.html (or: make serve).")
        if args.notify_success:
            notify("go card updated", result["message"], args, "ok", None)
        return finish(0)


if __name__ == "__main__":
    sys.exit(main())
