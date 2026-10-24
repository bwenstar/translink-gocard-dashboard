"""Several go cards: configuration, per-card login, the update loop and the build."""
import contextlib
import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import fakes
import cards
import build
import fetch_history as fh
import update
from test_fetch_history import FakeSiteCase, no_gocard_env
from test_update import FakeSteps


class Config(unittest.TestCase):
    def test_paths_and_labels(self):
        d = pathlib.Path("/x/data")
        self.assertEqual(cards.paths(1, d), (d / "history.csv", d / "raw"))
        self.assertEqual(cards.paths(2, d), (d / "history.card2.csv", d / "raw-card2"))
        env = {"USERNAME": "1", "USERNAME_2": "2", "CARD_NAME_2": "Partner", "GOCARD_CARD_4": "4"}
        got = [(c.n, c.label) for c in cards.configured(env, d)]
        self.assertEqual(got, [(1, "Card 1"), (2, "Partner"), (4, "Card 4")])

    def test_present_needs_no_credentials(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "history.csv").write_text("x")
        (d / "history.card3.csv").write_text("x")
        self.assertEqual([c.n for c in cards.present({}, d)], [1, 3])


class SecondCardLogin(FakeSiteCase):
    def test_card_2_uses_its_own_credentials_and_files(self):
        self.env.write_text(f"USERNAME=5555666677778888\nPASSWORD=other\nUSERNAME_2={fakes.CARD}\nPASSWORD_2={fakes.PASSWORD}\n")
        self.env.chmod(0o600)
        site = self.site()
        out = io.StringIO()
        history = self.dir / "history.card2.csv"
        argv = ["fetch_history.py", "--card", "2", "--history", str(history), "--raw", str(self.dir / "raw-card2"),
                "--env", str(self.env), "--years", "0.2", "--no-raw"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            fh.main()
        self.assertEqual(site.posts[0]["cardNum"], fakes.CARD)          # card 2's number, not card 1's
        self.assertTrue(history.exists())
        self.assertFalse((self.dir / "history.csv").exists())           # card 1's file untouched

    def test_missing_second_card_number_names_the_key(self):
        self.env.write_text("USERNAME=1111222233334444\nPASSWORD=x\n")
        with no_gocard_env(), self.assertRaises(SystemExit) as e:
            fh.credentials(self.env, n=2)
        self.assertIn("USERNAME_2", str(e.exception))


class UpdateLoop(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self.env.write_text("USERNAME=1111222233334444\nPASSWORD=a\nUSERNAME_2=5555666677778888\nPASSWORD_2=b\nCARD_NAME_2=Partner\n")
        self.env.chmod(0o600)
        (self.dir / "SEQ_GTFS.zip").write_text("zip")
        for name in ("history.csv", "history.card2.csv"):
            (self.dir / name).write_text("Date,Time,From,Time,To,Fare,Credit,Balance\n01/Jan/2026,08:00 AM,A,,,,,\n")
        for p in (mock.patch.object(update, "notify", lambda *a: None), mock.patch.object(update.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def run_update(self, steps):
        argv = ["--history", str(self.dir / "history.csv"), "--raw", str(self.dir / "raw"), "--env", str(self.env),
                "--gtfs-zip", str(self.dir / "SEQ_GTFS.zip"), "--state", str(self.dir)]
        with mock.patch.object(update, "run_step", steps), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = update.main(argv)
        return code, json.loads((self.dir / "last-update.json").read_text())

    def test_each_card_is_fetched_then_one_build(self):
        steps = FakeSteps(fetch=(0, "Downloaded 4 rows: 2 new, 0 updated.\n", ""))
        code, status = self.run_update(steps)
        self.assertEqual(code, 0)
        self.assertEqual(steps.names(), ["fetch", "fetch", "build"])
        fetches = [a for n, a in steps.calls if n == "fetch"]
        self.assertEqual([a[a.index("--card") + 1] for a in fetches], ["1", "2"])
        self.assertIn(str(self.dir / "history.card2.csv"), fetches[1])
        build_argv = steps.argv("build")
        self.assertEqual(build_argv.count("--history"), 2)
        self.assertEqual(status["new_rows"], 4)

    def test_one_card_failing_still_rebuilds_and_pauses_only_that_card(self):
        def steps(name, argv, log):
            argv = [str(a) for a in argv]
            steps.calls.append((name, argv))
            if name == "fetch" and argv[argv.index("--card") + 1] == "2":
                return 1, "", "Login failed - check the card number and password in .env\n"
            return 0, "Downloaded 1 rows: 1 new, 0 updated.\n", ""
        steps.calls = []
        code, status = self.run_update(steps)
        self.assertEqual(code, 1)                                         # reported as a failure...
        self.assertIn("build", [n for n, _ in steps.calls])               # ...but card 1's trips were built
        self.assertIn("Partner:", status["message"])
        self.assertTrue((self.dir / ".login-failed-card2").exists())
        self.assertFalse((self.dir / ".login-failed").exists())

        again = FakeSteps()                                               # next week: card 2 stays paused
        code, status = self.run_update(again)
        cards_fetched = [a[a.index("--card") + 1] for n, a in again.calls if n == "fetch"]
        self.assertEqual(cards_fetched, ["1"])
        self.assertIn("build", again.names())


class Build(unittest.TestCase):
    def test_two_cards(self):
        d = pathlib.Path(tempfile.mkdtemp())
        zip_path, stops_txt = fakes.make_gtfs(d / "gtfs")
        rows1 = ["04/Mar/2025,08:00 AM,Alpha Road - 1,08:15 AM,Beta Street - 2,$ 0.50,,$ 9.50",
                 "05/Mar/2025,10:00 AM,Auto top up(Alpha Road - 1),,,,$ 20.00,$ 29.50"]
        # card 2's first leg is a transfer 10 min after card 1's leg: it must not join card 1's journey
        rows2 = ["04/Mar/2025,08:25 AM,Beta Street - 2 (Touch on Transfer),08:40 AM,Alpha Road - 1,$ 0.00,,$ 5.00",
                 "06/Mar/2025,09:00 AM,Fern ferry terminal,09:20 AM,Gum ferry terminal,$ 0.50,,$ 4.50"]
        hdr = fakes.HEADER + "\n"
        (d / "history.csv").write_text(hdr + "\n".join(" " + r for r in rows1) + "\n")
        (d / "history.card2.csv").write_text(hdr + "\n".join(" " + r for r in rows2) + "\n")
        (d / ".env").write_text("CARD_NAME=Mine\nCARD_NAME_2=Partner\n")
        argv = ["build.py", "--history", str(d / "history.csv"), "--history", str(d / "history.card2.csv"),
                "--env", str(d / ".env"), "--stops", str(d / "stops.json"), "--gtfs", str(stops_txt),
                "--gtfs-zip", str(zip_path), "--out", str(d / "data.js"), "--checks", str(d / "checks.json")]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            build.main()
        text = (d / "data.js").read_text()
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        self.assertEqual(data["cards"], [{"n": 1, "label": "Mine"}, {"n": 2, "label": "Partner"}])
        legs = data["legs"]
        self.assertEqual([l["card"] for l in legs], [1, 2, 2])
        self.assertNotEqual(legs[0]["journey"], legs[1]["journey"])
        self.assertEqual(len({l["journey"] for l in legs}), 3)
        self.assertEqual([t["card"] for t in data["topups"]], [1])


if __name__ == "__main__":
    unittest.main()
