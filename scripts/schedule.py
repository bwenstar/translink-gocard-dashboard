#!/usr/bin/env python3
"""Run scripts/update.py automatically, weekly by default.

  python3 scripts/schedule.py install [--when "Mon *-*-* 07:00"]   # or: make schedule
  python3 scripts/schedule.py status                              # or: make schedule-status
  python3 scripts/schedule.py remove                              # or: make unschedule

On Linux with systemd this installs a user timer (~/.config/systemd/user/gocard-update.*).
Persistent=true means a run missed while the computer was off happens at the next boot.
User timers run while you're logged in; for runs when you're not, enable lingering
(`loginctl enable-linger`). Without systemd (macOS, others) it prints a crontab line to
add yourself. On Windows, point Task Scheduler at `python scripts\\update.py`.
"""
import argparse
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = "gocard-update"
UNIT_DIR = pathlib.Path.home() / ".config" / "systemd" / "user"


def service_unit(python=sys.executable, root=ROOT):
    return f"""[Unit]
Description=Update the go card travel dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={python} {root / "scripts" / "update.py"}
"""


def timer_unit(when="Mon *-*-* 07:00"):
    return f"""[Unit]
Description=Weekly go card dashboard update

[Timer]
OnCalendar={when}
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
"""


def cron_line(python=sys.executable, root=ROOT):
    return f"0 7 * * 1 cd {root} && {python} scripts/update.py >/dev/null 2>&1"


def have_systemd():
    return sys.platform.startswith("linux") and shutil.which("systemctl") and \
        subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True).returncode in (0, 1)


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], text=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["install", "status", "remove"])
    ap.add_argument("--when", default="Mon *-*-* 07:00", help='systemd OnCalendar (default: "Mon *-*-* 07:00", weekly)')
    args = ap.parse_args(argv)

    if not have_systemd():
        if args.action == "install":
            print("No systemd here. Add this line with `crontab -e` (Mondays 07:00):\n")
            print("  " + cron_line())
        else:
            print("No systemd here; see `crontab -l` for the cron entry.")
        return 0

    if args.action == "install":
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        (UNIT_DIR / f"{NAME}.service").write_text(service_unit())
        (UNIT_DIR / f"{NAME}.timer").write_text(timer_unit(args.when))
        systemctl("daemon-reload")
        systemctl("enable", "--now", f"{NAME}.timer")
        print(f"Installed. Next run:")
        systemctl("list-timers", f"{NAME}.timer", "--no-pager")
        print(f"\nLogs: journalctl --user -u {NAME} · data/update.log   Remove: make unschedule")
    elif args.action == "status":
        systemctl("list-timers", f"{NAME}.timer", "--no-pager")
        systemctl("status", f"{NAME}.service", "--no-pager", "--lines=5")
    else:
        systemctl("disable", "--now", f"{NAME}.timer")
        for ext in ("service", "timer"):
            (UNIT_DIR / f"{NAME}.{ext}").unlink(missing_ok=True)
        systemctl("daemon-reload")
        print("Removed the weekly update.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
