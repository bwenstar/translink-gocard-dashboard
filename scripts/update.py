#!/usr/bin/env python3
"""Bring everything up to date in one go. This is what `make update` and the weekly timer run.

  1. refresh Translink's timetable (fetch_gtfs.py) if it's older than --gtfs-days
  2. download new go card trips (fetch_history.py). On a first run it imports data/raw/
     if you have exports there, otherwise it downloads the whole history (--all).
  3. rebuild the dashboard (build.py)

Every run appends to data/update.log and writes data/last-update.json, plus a copy for
the dashboard header (dashboard/status.js), so a failed run shows there even though the
dashboard wasn't rebuilt. update.log and cron.log (the README's cron line) are trimmed to
their newest runs once they pass --log-max-kb, so they can't fill a small SD card. On failure it
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
sys.path.insert(0, str(SCRIPTS))
import cards  # noqa: E402

BETWEEN_CARDS_S = 5  # pause between one card's session and the next login


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


def fetch_args(card, env):
    """The fetch_history.py call for one card: incremental, or the right first-run mode."""
    common = ["--card", card.n, "--history", card.history, "--raw", card.raw, "--env", env]
    if has_rows(card.history):
        return [SCRIPTS / "fetch_history.py", *common], None
    if any(card.raw.rglob("*.csv")) if card.raw.exists() else False:
        return [SCRIPTS / "fetch_history.py", *common, "--import", card.raw], f"importing {card.raw.name}/ (first run)"
    return [SCRIPTS / "fetch_history.py", *common, "--all"], "downloading the whole history (first run)"


def trim_log(path, max_bytes, marker="\n"):
    """Once a log passes max_bytes, keep about its newest half, starting at a marker (a run's
    header, or a line). Rewritten in place, so an append-mode writer such as cron's >> keeps working."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    with open(path, "r+b") as f:
        f.seek(size - max_bytes // 2)
        tail = f.read()
        cut = tail.find(marker.encode())
        tail = tail[cut + 1:] if cut >= 0 else b""
        f.seek(0)
        f.write(tail)
        f.truncate()


def write_dashboard_status(path, result):
    """The last run's outcome for the dashboard header. Messages and card labels only, no trips."""
    keep = ("started", "finished", "ok", "step", "kind", "message", "new_rows")
    status = {k: result.get(k) for k in keep}
    status["cards"] = [{k: c.get(k) for k in ("card", "label", "ok", "kind")} for c in result.get("cards", [])]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("window.UPDATE_STATUS = " + json.dumps(status, ensure_ascii=False) + ";\n")


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
    ap.add_argument("--log-max-kb", type=int, default=1024,
                    help="trim update.log and cron.log to their newest runs past this size (default 1024)")
    ap.add_argument("--status-js", type=pathlib.Path, default=ROOT / "dashboard" / "status.js",
                    help="the last run's outcome for the dashboard header (default: dashboard/status.js)")
    args = ap.parse_args(argv)

    state = args.state
    status_file = state / "last-update.json"
    marker_for = lambda card: state / (".login-failed" if card.n == 1 else f".login-failed-card{card.n}")
    state.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now().isoformat(timespec="seconds")
    result = {"started": started, "ok": False, "step": None, "kind": None, "message": "", "new_rows": None}

    with single_instance(state / ".update.lock") as mine, open(state / "update.log", "a", encoding="utf-8") as log:
        if not mine:
            print("Another update is already running; nothing to do.")
            return 0
        trim_log(state / "update.log", args.log_max_kb * 1024, marker="\n=== ")
        trim_log(state / "cron.log", args.log_max_kb * 1024)
        log.write(f"\n=== {started} ===\n")

        def fail(step, kind, message):
            result.update(step=step, kind=kind, message=message)
            print(f"\nUpdate failed at {step}: {message}", file=sys.stderr)
            notify("go card update failed", message, args, "failed", kind)
            return 1

        def finish(code):
            result["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            status_file.write_text(json.dumps(result, indent=2) + "\n")
            write_dashboard_status(args.status_js, result)
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

        # 2. go card history, one card at a time (each has its own login and its own pause)
        tracked = cards.configured(cards.read_env(args.env), args.history.parent)
        tracked[0].history, tracked[0].raw = args.history, args.raw  # card 1: --history/--raw
        many = len(tracked) > 1
        name = lambda card: f"{card.label}: " if many else ""
        failures, fetched = [], 0
        result["cards"] = []
        for i, card in enumerate([] if args.no_fetch else tracked):
            if i:
                time.sleep(BETWEEN_CARDS_S)
            if many:
                print(f"\n--- {card.label} ---")
            marker = marker_for(card)
            env_changed = args.env.exists() and marker.exists() and args.env.stat().st_mtime > marker.stat().st_mtime
            entry = {"card": card.n, "label": card.label, "ok": False, "kind": None, "new_rows": None}
            result["cards"].append(entry)
            if marker.exists() and not (args.force or env_changed):
                entry.update(kind="login", message="skipped: the last login failed")
                failures.append(("login", f"{name(card)}Skipped: the last login failed. " + ADVICE["login"]))
                continue
            cmd, note = fetch_args(card, args.env)
            if note:
                print(f"Go card history: {note}")
            code, out, err = run_step("fetch", cmd, log)
            if code:
                kind = classify(err)
                if kind == "login":
                    marker.write_text(f"{started}\n{err.strip()}\n")
                last = (err.strip().splitlines() or ["unknown error"])[-1].rstrip()
                last += "" if last.endswith((".", "!", "?", ")")) else "."
                entry.update(kind=kind, message=last)
                failures.append((kind, f"{name(card)}{last} {ADVICE[kind]}"))
                continue
            marker.unlink(missing_ok=True)
            m = re.search(r"(\d+) new", out)
            entry.update(ok=True, new_rows=int(m.group(1)) if m else None)
            fetched += 1
        if failures and not fetched:  # nothing new to build from
            kinds = {k for k, _ in failures}
            return finish(fail("fetch", kinds.pop() if len(kinds) == 1 else "error", " ".join(msg for _, msg in failures)))
        counts = [c["new_rows"] for c in result["cards"] if c["new_rows"] is not None]
        result["new_rows"] = sum(counts) if counts else None

        # 3. dashboard
        histories = [c.history for c in tracked if c.history.exists()] or [args.history]
        code, out, err = run_step("build", [SCRIPTS / "build.py", "--env", args.env,
                                            *[a for h in histories for a in ("--history", h)]], log)
        if code:
            last = (err.strip().splitlines() or ["unknown error"])[-1]
            return finish(fail("build", "error", last))

        n = result["new_rows"]
        if failures:  # some cards worked: the dashboard is rebuilt, but report the rest
            kinds = {k for k, _ in failures}
            return finish(fail("fetch", kinds.pop() if len(kinds) == 1 else "error",
                               "Dashboard rebuilt, but: " + " ".join(msg for _, msg in failures)))
        result.update(ok=True, step="done",
                      message="Dashboard rebuilt" + ("" if n is None else f", {n} new row{'s' if n != 1 else ''}"))
        print(f"\n{result['message']}. Open dashboard/index.html (or: make serve).")
        if args.notify_success:
            notify("go card updated", result["message"], args, "ok", None)
        return finish(0)


if __name__ == "__main__":
    sys.exit(main())
