"""make_example.py: the fictional commuter's card produces valid, self-consistent go card CSV."""
import datetime as dt
import random
import unittest

import fakes  # noqa: F401  (puts scripts/ on the path)
import build
import fetch_history
import make_example as mx
from history import parse_rows


def every_ten_minutes():
    """A fake timetable: every leg runs every 10 minutes, 05:00-23:00, every day, taking 20 minutes."""
    deps = [(m, m + 20) for m in range(300, 1380, 10)]
    return {leg: {d: deps for d in range(7)} for leg in mx.LEGS}


class Card(unittest.TestCase):
    def setUp(self):
        self.card = mx.Card(random.Random(1), every_ten_minutes())
        self.day = dt.date(2026, 3, 2)  # a Monday

    def test_boards_the_next_departure(self):
        arr = self.card.ride(self.day, "to_work", 7 * 60 + 31)
        self.assertEqual(arr, 7 * 60 + 40 + 20)
        day, on, frm, off, to, fare, credit = self.card.rows[0]
        self.assertIn(on, range(7 * 60 + 38, 7 * 60 + 41))  # tapped on just before 07:40
        self.assertEqual((frm, to, fare), ("Kedron Brook station", "Roma Street busway station", mx.FARE))

    def test_no_service_left_today(self):
        self.assertIsNone(self.card.ride(self.day, "to_work", 23 * 60 + 30))
        self.assertEqual(self.card.rows, [])

    def test_transfer_and_failed_touch_off(self):
        self.card.ride(self.day, "to_cc", 600)
        self.card.ride(self.day, "ferry_out", 640, transfer=True)
        self.card.ride(self.day, "ferry_back", 800, fail=True)
        rows = parse_rows(self.card.csv().splitlines())
        self.assertTrue(rows[1][2].endswith("(Touch on Transfer)"))
        self.assertEqual(rows[1][5], "$ 0.00")
        self.assertEqual((rows[2][3], rows[2][4], rows[2][5]), ("Unknown", "Failed to touch Off", "$ 5.00"))

    def test_auto_top_up_when_low(self):
        self.card.balance = 4.80
        self.card.ride(self.day, "to_work", 480)
        rows = parse_rows(self.card.csv().splitlines())
        self.assertTrue(rows[0][2].startswith("Auto top up("))
        self.assertEqual(rows[0][6], "$ 20.00")


class Simulation(unittest.TestCase):
    def setUp(self):
        self.tt = every_ten_minutes()
        self.start, self.end = dt.date(2025, 11, 3), dt.date(2026, 2, 27)

    def test_same_seed_same_file(self):
        a = mx.simulate(self.tt, self.start, self.end, 7).csv()
        b = mx.simulate(self.tt, self.start, self.end, 7).csv()
        c = mx.simulate(self.tt, self.start, self.end, 8).csv()
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_valid_go_card_csv_with_consistent_balances(self):
        text = mx.simulate(self.tt, self.start, self.end, 7).csv()
        self.assertIsNone(fetch_history.header_problem(text))
        rows = parse_rows(text.splitlines())
        bal = 20.00
        for r in rows:  # each row's balance = previous + credit - fare
            bal += (build.money(r[6]) or 0) - (build.money(r[5]) or 0)
            self.assertAlmostEqual(build.money(r[7]), bal, places=2, msg=r)
        self.assertGreaterEqual(min(build.money(r[7]) for r in rows), 0)
        legs, topups = build.parse_legs(rows)
        stops = {n for l in legs for n in (l["from"], l["to"]) if n}
        self.assertEqual(stops - set(mx.STOPS), set())   # every stop has a mapping in examples/stops.json
        self.assertTrue(any(l["transfer"] for l in legs) and topups)

    def test_weekly_shape(self):
        legs, _ = build.parse_legs(parse_rows(mx.simulate(self.tt, self.start, self.end, 7).csv().splitlines()))
        days = {dt.date.fromisoformat(l["on"][:10]) for l in legs}
        workdays_on_leave = [d for d in days if dt.date(2025, 12, 22) <= d <= dt.date(2026, 1, 5) and d.weekday() < 5]
        self.assertEqual(workdays_on_leave, [])  # no commuting over the Christmas break (Sunday shopping is fine)
        gold_coast = [l for l in legs if l["to"] == "Broadbeach South light rail station"]
        self.assertTrue(gold_coast and all(dt.date.fromisoformat(l["on"][:10]).weekday() == 5 for l in gold_coast))


if __name__ == "__main__":
    unittest.main()
