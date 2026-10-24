"""fetch_history.py: credentials, date windows, login/export safety, and full runs against a fake site."""
import contextlib
import datetime as dt
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import fakes
import fetch_history as fh
import requests
from history import Master

GOCARD_VARS = ("GOCARD_CARD", "GOCARD_PASSWORD")


def no_gocard_env():
    """Patch os.environ so the developer's own GOCARD_* variables can't leak into a test."""
    env = {k: v for k, v in os.environ.items() if k not in GOCARD_VARS}
    return mock.patch.dict(os.environ, env, clear=True)


class Env(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"

    def write(self, text, mode=0o600):
        self.env.write_text(text)
        self.env.chmod(mode)

    def test_read_env_formats(self):
        self.write('# comment\nexport USERNAME="1111 2222 3333 4444"\nPASSWORD=\'p=ss word\'\n\nJUNK\n')
        self.assertEqual(fh.read_env(self.env), {"USERNAME": "1111 2222 3333 4444", "PASSWORD": "p=ss word"})

    def test_missing_file_is_empty(self):
        self.assertEqual(fh.read_env(self.dir / "nope"), {})

    def test_credentials_from_env_file(self):
        self.write("USERNAME=1111 2222 3333 4444\nPASSWORD=pw\n")
        with no_gocard_env():
            self.assertEqual(fh.credentials(self.env), ("1111222233334444", "pw"))

    def test_environment_overrides_file(self):
        self.write("USERNAME=1111222233334444\nPASSWORD=file\n")
        with no_gocard_env(), mock.patch.dict(os.environ, {"GOCARD_CARD": "5555666677778888", "GOCARD_PASSWORD": "env"}):
            self.assertEqual(fh.credentials(self.env), ("5555666677778888", "env"))

    def test_os_username_is_ignored(self):
        # USERNAME in the environment is usually your OS login name, never a card number
        self.write("PASSWORD=pw\n")
        with no_gocard_env(), mock.patch.dict(os.environ, {"USERNAME": "1111222233334444"}):
            with self.assertRaises(SystemExit) as e:
                fh.credentials(self.env)
        self.assertIn("No go card number", str(e.exception))

    def test_rejects_placeholder_and_bad_numbers(self):
        for card, msg in [(fh.EXAMPLE_CARD, "example card"), ("1234", "16 digits"), ("1111x22233334444", "16 digits")]:
            self.write(f"USERNAME={card}\nPASSWORD=pw\n")
            with no_gocard_env(), self.assertRaises(SystemExit) as e:
                fh.credentials(self.env)
            self.assertIn(msg, str(e.exception))

    def test_prompts_for_missing_password(self):
        self.write("USERNAME=1111222233334444\n")
        with no_gocard_env(), mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fh.getpass, "getpass", return_value="typed") as gp:
            self.assertEqual(fh.credentials(self.env), ("1111222233334444", "typed"))
        self.assertIn("...4444", gp.call_args[0][0])  # only the last 4 digits are shown

    def test_ask_ignores_stored_password(self):
        self.write("USERNAME=1111222233334444\nPASSWORD=stored\n")
        with no_gocard_env(), mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(fh.getpass, "getpass", return_value="typed"):
            self.assertEqual(fh.credentials(self.env, ask=True)[1], "typed")

    def test_no_password_without_terminal(self):
        self.write("USERNAME=1111222233334444\n")
        with no_gocard_env(), mock.patch.object(sys.stdin, "isatty", return_value=False), self.assertRaises(SystemExit):
            fh.credentials(self.env)

    @unittest.skipUnless(os.name == "posix", "file permissions")
    def test_warns_when_env_is_readable_by_others(self):
        for mode, warned in [(0o644, True), (0o600, False)]:
            self.write("x=1\n", mode)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                fh.warn_if_readable(self.env)
            self.assertEqual("chmod 600" in err.getvalue(), warned, oct(mode))


class Dates(unittest.TestCase):
    def test_parse_date_formats(self):
        self.assertEqual(fh.parse_date("24/10/2026"), dt.date(2026, 10, 24))
        self.assertEqual(fh.parse_date("2026-10-24"), dt.date(2026, 10, 24))
        with self.assertRaises(Exception):
            fh.parse_date("Oct 24")

    def test_windows_cover_range_exactly(self):
        start, end = dt.date(2020, 1, 1), dt.date(2026, 10, 24)
        wins = list(fh.windows_back(start, end))
        self.assertEqual(wins[0][1], end)            # newest first
        self.assertEqual(wins[-1][0], start)
        for (a0, a1), (b0, b1) in zip(wins, wins[1:]):
            self.assertEqual(b1, a0 - dt.timedelta(days=1))  # contiguous, no gaps or overlap
        self.assertTrue(all((w1 - w0).days + 1 <= fh.WINDOW_DAYS for w0, w1 in wins))
        days = sum((w1 - w0).days + 1 for w0, w1 in wins)
        self.assertEqual(days, (end - start).days + 1)

    def test_single_day_and_empty(self):
        d = dt.date(2026, 1, 1)
        self.assertEqual(list(fh.windows_back(d, d)), [(d, d)])
        self.assertEqual(list(fh.windows_back(d, d - dt.timedelta(days=1))), [])


class FakeSiteCase(unittest.TestCase):
    """Runs the real code against fakes.FakeSite with requests.Session and time.sleep patched."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self.env.write_text(f"USERNAME={fakes.CARD}\nPASSWORD={fakes.PASSWORD}\n")
        self.env.chmod(0o600)
        self.history, self.raw = self.dir / "history.csv", self.dir / "raw"

    def site(self, **kw):
        self.fake = fakes.FakeSite(**kw)
        for p in (mock.patch.object(fh.requests, "Session", self.fake.session), mock.patch.object(fh.time, "sleep"),
                  no_gocard_env()):
            p.start()
            self.addCleanup(p.stop)
        return self.fake

    def run_cli(self, *args):
        argv = ["fetch_history.py", "--history", str(self.history), "--raw", str(self.raw), "--env", str(self.env), *args]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            fh.main()
        return out.getvalue()


class Login(FakeSiteCase):
    def test_success(self):
        self.site()
        s = fh.login(fakes.CARD, fakes.PASSWORD)
        self.assertTrue(self.fake.logged_in)
        self.assertEqual(self.fake.posts, [{"cardOps": "Display", "cardNum": fakes.CARD, "pass": fakes.PASSWORD}])
        fh.logout(s)
        self.assertEqual(self.fake.logouts, 1)

    def test_wrong_password_tries_once(self):
        self.site()
        with self.assertRaises(SystemExit) as e:
            fh.login(fakes.CARD, "wrong")
        self.assertIn("Login failed", str(e.exception))
        self.assertEqual(len(self.fake.posts), 1)  # never retried: can't lock the card

    def test_changed_form_never_receives_credentials(self):
        self.site(login_page=fakes.LOGIN_PAGE.replace('name="pass"', 'name="password"'))
        with self.assertRaises(SystemExit) as e:
            fh.login(fakes.CARD, fakes.PASSWORD)
        self.assertIn("site has changed", str(e.exception))
        self.assertEqual(self.fake.posts, [])

    def test_redirect_to_another_host_stops(self):
        self.site(redirect_host="evil.example.com")
        with self.assertRaises(SystemExit) as e:
            fh.login(fakes.CARD, fakes.PASSWORD)
        self.assertIn("evil.example.com", str(e.exception))


class Export(FakeSiteCase):
    def setUp(self):
        super().setUp()
        self.today = dt.date.today()

    def logged_in(self, **kw):
        self.site(**kw)
        return fh.login(fakes.CARD, fakes.PASSWORD)

    def test_rows_and_count(self):
        s = self.logged_in(every_days=1)
        text, n = fh.export(s, self.today - dt.timedelta(days=9), self.today)
        self.assertEqual(n, 10)
        self.assertTrue(text.startswith(fakes.HEADER))

    def test_too_long_range_is_none(self):
        s = self.logged_in()
        self.assertIsNone(fh.export(s, self.today - dt.timedelta(days=300), self.today))

    def test_html_instead_of_csv_stops(self):
        s = self.logged_in(export_override=lambda a, b: "<html><body>Please log in</body></html>")
        with self.assertRaises(SystemExit) as e:
            fh.export(s, self.today, self.today)
        self.assertIn("web page instead of CSV", str(e.exception))

    def test_changed_columns_stop(self):
        s = self.logged_in(export_override=lambda a, b: "Date,Time,From,To,Fare\n01/Jan/2026,08:00 AM,A,B,$ 1.00\n")
        with self.assertRaises(SystemExit) as e:
            fh.export(s, self.today, self.today)
        self.assertIn("CSV columns", str(e.exception))

    def test_save_names_folder_by_dates(self):
        p = fh.save("x", dt.date(2026, 4, 28), dt.date(2026, 10, 24), self.raw)
        self.assertEqual(p.relative_to(self.raw).as_posix(), "260428-261024/Travel_history.csv")


class Runs(FakeSiteCase):
    def test_all_walks_back_to_first_trip(self):
        site = self.site(first_trip=dt.date.today() - dt.timedelta(days=500))
        out = self.run_cli("--all")
        rows = Master(self.history).rows
        self.assertEqual(len(rows), len(site.days))
        # stops after two empty windows before the first trip
        self.assertEqual(len(site.exports), len(list(fh.windows_back(site.first, dt.date.today()))) + 2)
        self.assertTrue(all((b - a).days + 1 <= 180 for a, b in site.exports))
        self.assertEqual(site.logouts, 1)
        self.assertEqual(len(list(self.raw.rglob("*.csv"))), len(site.exports) - 2)
        self.assertIn("...4444", out)
        self.assertNotIn(fakes.PASSWORD, out)

    def test_incremental_overlaps_a_week_and_adds_nothing_twice(self):
        site = self.site()
        self.run_cli("--all")
        n = len(Master(self.history).rows)
        site.exports.clear()
        out = self.run_cli()
        latest = Master(self.history).latest_date()
        self.assertEqual(site.exports[0][0], latest - dt.timedelta(days=fh.OVERLAP_DAYS))
        self.assertEqual(len(Master(self.history).rows), n)
        self.assertIn("0 new", out)

    def test_years_and_explicit_ranges(self):
        site = self.site(first_trip=dt.date(2024, 1, 1))
        self.run_cli("--from", "2024-03-01", "--to", "2025-02-28", "--no-raw")
        self.assertEqual(site.exports[-1][0], dt.date(2024, 3, 1))
        self.assertEqual(site.exports[0][1], dt.date(2025, 2, 28))
        self.assertFalse(self.raw.exists())  # --no-raw
        site.exports.clear()
        self.run_cli("--years", "1", "--no-raw")
        span = (site.exports[0][1] - site.exports[-1][0]).days
        self.assertAlmostEqual(span, 365, delta=2)

    def test_incremental_needs_existing_history(self):
        self.site()
        with self.assertRaises(SystemExit) as e:
            self.run_cli()
        self.assertIn("--all", str(e.exception))

    def test_from_after_to(self):
        self.site()
        with self.assertRaises(SystemExit):
            self.run_cli("--from", "2026-02-01", "--to", "2026-01-01")

    def test_import_needs_no_login(self):
        site = self.site()
        csv_file = self.dir / "manual.csv"
        csv_file.write_text("\n".join([fakes.HEADER, fakes.trip_row(dt.date(2025, 5, 1))]) + "\n")
        self.run_cli("--import", str(csv_file))
        self.assertEqual(len(Master(self.history).rows), 1)
        self.assertEqual(site.posts, [])

    def test_network_error_is_friendly(self):
        self.site()
        with mock.patch.object(fh, "login", side_effect=requests.ConnectionError("down")), \
                self.assertRaises(SystemExit) as e:
            self.run_cli("--all")
        self.assertIn("Couldn't reach go card online", str(e.exception))

    def test_check_site_reports_and_snapshots(self):
        self.site()
        out = self.run_cli("--check-site")
        self.assertIn("All checks passed", out)
        self.assertIn("6-month limit: unchanged", out)
        snap = next((self.dir / "site-snapshots").iterdir())
        self.assertEqual({p.name for p in snap.iterdir()},
                         {"login.html", "after-login.html", "history.html", "README.txt"})

    def test_check_site_notices_a_lifted_limit(self):
        self.site(max_days=10_000)
        self.assertIn("limit may have been lifted", self.run_cli("--check-site"))


if __name__ == "__main__":
    unittest.main()
