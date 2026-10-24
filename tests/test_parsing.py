"""Tests for CSV parsing, the master-file merge and the site-contract checks.

Run: python3 -m unittest discover tests

The sample rows are invented but use the exact formats go card online exports
(see docs/site-contract.json), including every row type the parser handles.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import build  # noqa: E402
import fetch_history  # noqa: E402
from history import Master, parse_rows  # noqa: E402

SAMPLE = """\
 Date,Time,From,Time,To,Fare,Credit,Balance
 03/Mar/2025,07:29 AM,Sample Road - 12,07:44 AM,Example St F/S Main St (Stop 9),$ 0.50,,$ 29.50
 03/Mar/2025,07:50 AM,Central Busway Platform 2 (Touch on Transfer),08:05 AM,Uni Lakes - Stop A,$ 0.00,,$ 29.50
 03/Mar/2025,17:20 PM,Uni Lakes - Stop A,17:58 PM,Sample Road - 12,$ 0.50,,$ 29.00
 04/Mar/2025,08:00 AM,Auto top up(Sample Road - 12),,,,$ 30.00,$ 59.00
 05/Mar/2025,09:15 AM,Top Up( Example Depot ),09:40 AM,Example St F/S Main St (Stop 9),$ 0.50,30.00,$ 88.50
 06/Mar/2025,12:00 PM,Adjustment(Sample Road - 12),,,,-30.00,$ 58.50
 07/Mar/2025,18:00 PM,Sample Road - 12,Unknown,Failed to touch Off,$ 5.00,,$ 53.50
 08/Mar/2025,,,Unknown,Failed to touch Off,$ 5.00,,$ 48.50
 09/Mar/2025,23:50 PM,Example St F/S Main St (Stop 9),00:20 AM,Sample Road - 12,$ 0.50,,$ 48.00
"""


def rows():
    return parse_rows(SAMPLE.splitlines())


class ParseLegs(unittest.TestCase):
    def setUp(self):
        self.legs, self.topups = build.parse_legs(rows())

    def test_counts(self):
        # 9 rows: 2 pure balance events (auto top-up, adjustment), the driver top-up is both
        self.assertEqual(len(self.legs), 7)
        self.assertEqual(len(self.topups), 3)

    def test_trip(self):
        l = self.legs[0]
        self.assertEqual((l["from"], l["to"], l["mins"], l["fare"], l["balance"]),
                         ("Sample Road - 12", "Example St F/S Main St (Stop 9)", 15, 0.5, 29.5))

    def test_redundant_pm_suffix(self):
        self.assertEqual(self.legs[2]["on"], "2025-03-03T17:20:00")
        self.assertEqual(self.legs[2]["mins"], 38)

    def test_transfer_joins_journey(self):
        a, b, c = self.legs[:3]
        self.assertTrue(b["transfer"])
        self.assertEqual(b["from"], "Central Busway Platform 2")
        self.assertEqual(a["journey"], b["journey"])
        self.assertNotEqual(b["journey"], c["journey"])

    def test_top_up_kinds(self):
        kinds = [(t["kind"], t["amount"]) for t in self.topups]
        self.assertEqual(kinds, [("auto", 30.0), ("manual", 30.0), ("adjustment", -30.0)])
        self.assertEqual(self.topups[0]["stop"], "Sample Road - 12")

    def test_driver_top_up_is_also_a_trip(self):
        l = self.legs[3]
        self.assertIsNone(l["from"])  # boarding stop isn't recorded
        self.assertEqual(l["to"], "Example St F/S Main St (Stop 9)")
        self.assertEqual(self.topups[1]["balance"], 89.0)  # balance before that trip's fare

    def test_failed_touch_off(self):
        l = self.legs[4]
        self.assertTrue(l["failedTouchOff"])
        self.assertIsNone(l["to"])
        self.assertIsNone(l["mins"])
        self.assertEqual(l["fare"], 5.0)

    def test_penalty_with_no_time_or_stop(self):
        l = self.legs[5]
        self.assertTrue(l["timeUnknown"] and l["failedTouchOff"])
        self.assertIsNone(l["from"])
        self.assertEqual(l["on"], "2025-03-08T00:00:00")

    def test_trip_past_midnight(self):
        l = self.legs[6]
        self.assertEqual(l["off"], "2025-03-10T00:20:00")
        self.assertEqual(l["mins"], 30)


class Money(unittest.TestCase):
    def test_formats(self):
        for s, v in [("$ 0.50", 0.5), ("30.00", 30.0), ("-30.00", -30.0), ("-$ 2.00", -2.0),
                     ("$ 1,234.50", 1234.5), ("", None)]:
            self.assertEqual(build.money(s), v, s)


class MasterMerge(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "history.csv"

    def test_overlap_counts_once(self):
        m = Master(self.path)
        self.assertEqual(m.merge(rows()), (9, 0))
        self.assertEqual(m.merge(rows()[3:]), (0, 0))  # an overlapping download
        self.assertEqual(len(m.rows), 9)

    def test_open_trip_completes_but_never_reverts(self):
        open_ = ["10/Mar/2025", "08:00 AM", "Sample Road - 12", "", "", "", "", "$ 48.00"]
        done = ["10/Mar/2025", "08:00 AM", "Sample Road - 12", "08:20 AM", "Uni Lakes - Stop A", "$ 0.50", "", "$ 47.50"]
        m = Master(self.path)
        self.assertEqual(m.merge([open_]), (1, 0))
        self.assertEqual(m.merge([done]), (0, 1))
        self.assertEqual(m.merge([open_]), (0, 0))
        self.assertEqual(list(m.rows.values()), [done])

    def test_genuine_repeats_kept(self):
        r = rows()[0]
        self.assertEqual(Master(self.path).merge([r, r]), (2, 0))

    def test_save_load_sorted(self):
        m = Master(self.path)
        m.merge(list(reversed(rows())))
        m.save()
        again = Master(self.path)
        self.assertEqual(len(again.rows), 9)
        self.assertEqual(next(iter(again.rows.values()))[0], "03/Mar/2025")
        self.assertEqual(str(again.latest_date()), "2025-03-09")


LOGIN_HTML = """<html><body>
<form id="search" action="/search" method="get"><input type="text" name="query"></form>
<form id="login-form" action="/webtix/welcome/welcome.do" method="post">
  <input type="hidden" name="cardOps" value="Display">
  <input type="text" name="cardNum" id="CardNumber">
  <input type="password" name="pass" id="Password">
  <input type="submit" value="Login">
</form></body></html>"""


class SiteContract(unittest.TestCase):
    def test_login_form_matches(self):
        self.assertEqual(fetch_history.login_form_problems(LOGIN_HTML), [])

    def test_detects_changes(self):
        cases = {
            "renamed field": LOGIN_HTML.replace('name="pass"', 'name="password"'),
            "new token": LOGIN_HTML.replace("</form></body>", '<input type="hidden" name="csrf" value="x"></form></body>'),
            "moved": LOGIN_HTML.replace('action="/webtix/welcome/welcome.do"', 'action="/login"'),
            "gone": LOGIN_HTML.replace('id="login-form"', 'id="sso"'),
        }
        for name, html in cases.items():
            self.assertTrue(fetch_history.login_form_problems(html), name)

    def test_csv_header(self):
        self.assertIsNone(fetch_history.header_problem(SAMPLE))
        self.assertIsNotNone(fetch_history.header_problem("Date,Time,From,To,Fare\n"))


class StopNames(unittest.TestCase):
    GTFS = {
        "1": {"stop_id": "1", "stop_name": "Main St at Example, stop 9", "stop_lat": "-27.47", "stop_lon": "153.02"},
        "2": {"stop_id": "2", "stop_name": "Sample Rd at Hill, stop 12", "stop_lat": "-27.49", "stop_lon": "153.06"},
        "3": {"stop_id": "3", "stop_name": "Central busway station, platform 2", "stop_lat": "-27.47", "stop_lon": "153.03"},
        "4": {"stop_id": "4", "stop_name": "Central busway station, platform 3", "stop_lat": "-27.47", "stop_lon": "153.03"},
    }

    def test_stop_number_and_platform(self):
        self.assertEqual(build.guess_stop("Sample Road - 12", self.GTFS)[0], "2")
        self.assertEqual(build.guess_stop("Central Busway Platform 2", self.GTFS)[0], "3")
        self.assertEqual(build.guess_stop("Main St (St 9)", self.GTFS)[0], "1")


if __name__ == "__main__":
    unittest.main()
