"""update.py and schedule.py: step order, first runs, failure handling, login pause, notifications."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import fakes  # noqa: F401  (puts scripts/ on the path)
import schedule
import update


class FakeSteps:
    """Stands in for update.run_step. results: {step name: (code, stdout, stderr)}."""

    def __init__(self, **results):
        self.results, self.calls = results, []

    def __call__(self, name, argv, log):
        self.calls.append((name, [str(a) for a in argv]))
        return self.results.get(name, (0, "", ""))

    def names(self):
        return [n for n, _ in self.calls]

    def argv(self, name):
        return next(a for n, a in self.calls if n == name)


class Update(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.history, self.raw, self.env = self.dir / "history.csv", self.dir / "raw", self.dir / ".env"
        self.gtfs = self.dir / "SEQ_GTFS.zip"
        self.history.write_text("Date,Time,From,Time,To,Fare,Credit,Balance\n01/Jan/2026,08:00 AM,A,,,,,\n")
        self.env.write_text("USERNAME=1111222233334444\nPASSWORD=x\n")
        self.gtfs.write_text("zip")
        self.notes = []
        p = mock.patch.object(update, "notify", lambda title, msg, args, status, kind: self.notes.append((status, kind, msg)))
        p.start()
        self.addCleanup(p.stop)

    def run_update(self, steps, *extra):
        argv = ["--history", str(self.history), "--raw", str(self.raw), "--env", str(self.env),
                "--gtfs-zip", str(self.gtfs), "--state", str(self.dir), "--status-js", str(self.dir / "status.js"), *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(update, "run_step", steps), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = update.main(argv)
        self.status = json.loads((self.dir / "last-update.json").read_text())
        js = (self.dir / "status.js").read_text()
        self.dashboard_status = json.loads(js.removeprefix("window.UPDATE_STATUS = ").removesuffix(";\n"))
        return code, out.getvalue() + err.getvalue()

    def test_normal_run(self):
        steps = FakeSteps(fetch=(0, "Downloaded 12 rows: 3 new, 0 updated. 1278 rows in history.csv\n", ""))
        code, out = self.run_update(steps)
        self.assertEqual(code, 0)
        self.assertEqual(steps.names(), ["fetch", "build"])       # timetable is fresh: not refreshed
        self.assertNotIn("--all", steps.argv("fetch"))            # incremental
        self.assertEqual((self.status["ok"], self.status["new_rows"]), (True, 3))
        self.assertEqual(self.notes, [])                          # silent on success by default
        self.assertIn("=== ok", (self.dir / "update.log").read_text())

    def test_dashboard_gets_the_outcome_even_when_not_rebuilt(self):
        self.run_update(FakeSteps(fetch=(0, "Downloaded 1 rows: 1 new, 0 updated.\n", "")))
        self.assertEqual((self.dashboard_status["ok"], self.dashboard_status["new_rows"]), (True, 1))
        self.run_update(FakeSteps(fetch=(1, "", "Couldn't reach go card online\n")))
        self.assertEqual((self.dashboard_status["ok"], self.dashboard_status["kind"]), (False, "network"))
        self.assertIn("next run will try again", self.dashboard_status["message"])
        self.assertEqual(set(self.dashboard_status["cards"][0]), {"card", "label", "ok", "kind"})

    def test_stale_or_missing_timetable_is_refreshed(self):
        old = time.time() - 40 * 86400
        os.utime(self.gtfs, (old, old))
        steps = FakeSteps()
        self.run_update(steps)
        self.assertEqual(steps.names(), ["gtfs", "fetch", "build"])

    def test_timetable_refresh_failure_carries_on_with_the_old_one(self):
        old = time.time() - 40 * 86400
        os.utime(self.gtfs, (old, old))
        code, _ = self.run_update(FakeSteps(gtfs=(1, "", "timeout")))
        self.assertEqual(code, 0)

    def test_no_timetable_at_all_is_fatal(self):
        self.gtfs.unlink()
        code, _ = self.run_update(FakeSteps(gtfs=(1, "", "timeout")))
        self.assertEqual((code, self.status["step"]), (1, "gtfs"))

    def test_first_run_imports_raw_exports_or_downloads_everything(self):
        self.history.unlink()
        steps = FakeSteps()
        self.run_update(steps)
        self.assertIn("--all", steps.argv("fetch"))
        (self.raw / "a").mkdir(parents=True)
        (self.raw / "a" / "Travel_history.csv").write_text("x")
        steps = FakeSteps()
        self.run_update(steps)
        self.assertIn("--import", steps.argv("fetch"))

    def test_login_failure_pauses_later_runs_until_env_changes(self):
        code, out = self.run_update(FakeSteps(fetch=(1, "", "Login failed - check the card number and password in .env\n")))
        self.assertEqual((code, self.status["kind"]), (1, "login"))
        self.assertTrue((self.dir / ".login-failed").exists())
        self.assertEqual(self.notes[-1][:2], ("failed", "login"))

        steps = FakeSteps()                                        # e.g. next week's timer run
        code, _ = self.run_update(steps)
        self.assertEqual(code, 1)
        self.assertNotIn("fetch", steps.names())                   # did not try to log in again
        self.assertIn("Skipped", self.status["message"])

        steps = FakeSteps()
        self.assertEqual(self.run_update(steps, "--force")[0], 0)  # --force tries anyway
        self.assertFalse((self.dir / ".login-failed").exists())    # success clears the pause

    def test_fixed_env_resumes(self):
        self.run_update(FakeSteps(fetch=(1, "", "Login failed\n")))
        later = time.time() + 5
        os.utime(self.env, (later, later))                         # you edited .env
        steps = FakeSteps()
        self.assertEqual(self.run_update(steps)[0], 0)
        self.assertIn("fetch", steps.names())

    def test_failure_kinds(self):
        for err, kind in [("Translink's site has changed since ... (login form has new fields: csrf)", "site-changed"),
                          ("Couldn't reach go card online (ReadTimeout).", "network"),
                          ("Traceback ... KeyError", "error")]:
            code, _ = self.run_update(FakeSteps(fetch=(1, "", err + "\n")))
            self.assertEqual((code, self.status["kind"]), (1, kind), err)
            self.assertFalse((self.dir / ".login-failed").exists())  # only login failures pause runs

    def test_failed_fetch_does_not_rebuild(self):
        steps = FakeSteps(fetch=(1, "", "Couldn't reach go card online\n"))
        self.run_update(steps)
        self.assertNotIn("build", steps.names())

    def test_build_failure_is_reported(self):
        code, _ = self.run_update(FakeSteps(build=(1, "", "Missing data/gtfs/stops.txt\n")))
        self.assertEqual((code, self.status["step"]), (1, "build"))
        self.assertEqual(self.notes[-1][0], "failed")

    def test_no_fetch_and_success_notification(self):
        steps = FakeSteps()
        self.run_update(steps, "--no-fetch", "--notify-success")
        self.assertEqual(steps.names(), ["build"])
        self.assertEqual(self.notes[-1][0], "ok")

    def test_one_run_at_a_time(self):
        import fcntl
        with open(self.dir / ".update.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            steps = FakeSteps()
            out = io.StringIO()
            with mock.patch.object(update, "run_step", steps), contextlib.redirect_stdout(out):
                code = update.main(["--state", str(self.dir), "--history", str(self.history), "--gtfs-zip", str(self.gtfs)])
        self.assertEqual((code, steps.calls), (0, []))
        self.assertIn("already running", out.getvalue())


class RealRunStep(unittest.TestCase):
    """The real run_step (the Update tests fake it): paths as arguments, output captured and logged."""

    def test_runs_a_script_and_logs(self):
        d = pathlib.Path(tempfile.mkdtemp())
        script = d / "hello.py"
        script.write_text("import sys; print('out', sys.argv[1]); print('err', file=sys.stderr); sys.exit(3)")
        log = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code, out, err = update.run_step("x", [script, d / "arg"], log)
        self.assertEqual((code, out.strip(), err.strip()), (3, f"out {d / 'arg'}", "err"))
        self.assertIn(f"$ {script} {d / 'arg'}", log.getvalue())

    def test_crash_is_notified(self):
        sent = []
        with mock.patch.object(update, "_main", side_effect=RuntimeError("boom")), \
                mock.patch.object(update, "notify", lambda *a: sent.append(a)), self.assertRaises(RuntimeError):
            update.main([])
        self.assertEqual(sent[0][0], "go card update crashed")


class RealMessages(unittest.TestCase):
    """classify() must keep matching fetch_history.py's actual exit messages."""

    def test_classifies_fetch_history_messages(self):
        import fetch_history as fh
        env = pathlib.Path(tempfile.mkdtemp()) / ".env"
        env.write_text(f"USERNAME={fh.EXAMPLE_CARD}\nPASSWORD=x\n")
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("GOCARD_")}, clear=True), \
                self.assertRaises(SystemExit) as e:
            fh.credentials(env)
        self.assertEqual(update.classify(str(e.exception)), "login")
        self.assertEqual(update.classify(fh.SITE_CHANGED.format("login form has new fields: csrf")), "site-changed")
        with mock.patch.object(fh, "run", side_effect=fh.requests.ConnectionError()), self.assertRaises(SystemExit) as e:
            fh.main()
        self.assertEqual(update.classify(str(e.exception)), "network")


class NotifyCommand(unittest.TestCase):
    def test_command_gets_status_in_its_environment(self):
        out = pathlib.Path(tempfile.mkdtemp()) / "got"
        args = mock.Mock(no_desktop=True, notify_cmd=f'echo "$GOCARD_STATUS|$GOCARD_KIND|$GOCARD_MESSAGE" > {out}')
        update.notify("go card update failed", "Login failed.", args, "failed", "login")
        self.assertEqual(out.read_text().strip(), "failed|login|go card update failed: Login failed.")


class Schedule(unittest.TestCase):
    def test_units(self):
        svc = schedule.service_unit(python="/usr/bin/python3", root=pathlib.Path("/home/x/gocard"))
        self.assertIn("ExecStart=/usr/bin/python3 /home/x/gocard/scripts/update.py", svc)
        self.assertIn("WorkingDirectory=/home/x/gocard", svc)
        timer = schedule.timer_unit("Sun *-*-* 20:00")
        self.assertIn("OnCalendar=Sun *-*-* 20:00", timer)
        self.assertIn("Persistent=true", timer)                    # catches up after the computer was off

    def test_cron_line(self):
        self.assertEqual(schedule.cron_line(python="python3", root=pathlib.Path("/srv/g")),
                         "0 7 * * 1 cd /srv/g && python3 scripts/update.py >/dev/null 2>&1")


if __name__ == "__main__":
    unittest.main()
