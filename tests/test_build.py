"""build.py: times, stop-name matching, stop modes, and a full build on the synthetic feed."""
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
import build


class Times(unittest.TestCase):
    def test_24h_with_redundant_suffix(self):
        d = dt.date(2026, 1, 5)
        cases = {"17:20 PM": (17, 20), "07:05 AM": (7, 5), "00:20 AM": (0, 20), "12:30 PM": (12, 30), "9:00": (9, 0)}
        for s, (h, m) in cases.items():
            self.assertEqual(build.parse_time(d, s), dt.datetime(2026, 1, 5, h, m), s)
        for s in ("", "Unknown", None):
            self.assertIsNone(build.parse_time(d, s))


def leg(on, frm, to, fare=0.5, journey=None, transfer=False, card=1):
    return {"on": on, "from": frm, "to": None if to == "FAILED" else to, "fare": 5.0 if to == "FAILED" else fare,
            "failedTouchOff": to == "FAILED", "transfer": transfer, "journey": journey, "card": card}


class FailedTouchOffs(unittest.TestCase):
    def guess(self, legs, where=None):
        for i, l in enumerate(legs):
            l["journey"] = l["journey"] if l["journey"] is not None else i
        build.guess_failed_touch_offs(legs, where or (lambda n: None))
        return next(l for l in legs if l["failedTouchOff"])

    def test_usual_destination_around_that_time(self):
        legs = [leg(f"2026-01-0{d}T17:30", "A", "Home", fare=0.5) for d in range(1, 4)]
        legs += [leg("2026-01-04T09:00", "A", "Gym"), leg("2026-01-05T17:40", "A", "FAILED")]
        f = self.guess(legs)
        self.assertEqual((f["likelyTo"], f["usualFare"]), ("Home", 0.5))
        self.assertIn("3 of your 3 trips from here around this time", f["likelyWhy"])

    def test_next_leg_of_the_same_journey_wins(self):
        legs = [leg(f"2026-01-0{d}T17:30", "A", "Home") for d in range(1, 4)]
        legs += [leg("2026-01-05T17:40", "A", "FAILED", journey=9), leg("2026-01-05T18:00", "Hub", "Home", journey=9, transfer=True)]
        self.assertEqual(self.guess(legs)["likelyTo"], "Hub")

    def test_next_tap_on_when_there_is_no_pattern(self):
        legs = [leg("2026-01-05T17:40", "A", "FAILED"), leg("2026-01-06T08:00", "Home", "A")]
        f = self.guess(legs)
        self.assertEqual((f["likelyTo"], f["likelyWhy"]), ("Home", "where you next tapped on"))
        legs = [leg("2026-01-05T17:40", "A", "FAILED"), leg("2026-01-08T08:00", "Home", "A")]  # days later: no
        self.assertNotIn("likelyTo", self.guess(legs))

    def test_a_stop_near_the_tap_on_is_not_a_destination(self):
        where = {"A": (-27.0, 153.0), "A opposite": (-27.0005, 153.0), "Home": (-27.1, 153.1)}.get
        legs = [leg("2026-01-05T17:40", "A", "FAILED"), leg("2026-01-05T19:00", "A opposite", "Home")]
        self.assertNotIn("likelyTo", self.guess(legs, where))

    def test_other_cards_are_ignored(self):
        legs = [leg(f"2026-01-0{d}T17:30", "A", "Elsewhere", card=2) for d in range(1, 4)]
        legs += [leg("2026-01-05T17:40", "A", "FAILED")]
        self.assertNotIn("likelyTo", self.guess(legs))

    def test_usual_fare(self):
        legs = [leg("2025-01-01T17:30", "A", "Home", fare=3.0), leg("2025-01-02T17:30", "A", "Home", fare=3.2),
                leg("2025-01-03T17:30", "A", "FAILED")]
        self.assertEqual(self.guess(legs)["usualFare"], 3.2)              # median of the same trip
        legs = [leg("2025-01-01T17:30", "B", "C", fare=1.0), leg("2025-01-02T17:30", "A", "FAILED"),
                leg("2025-01-03T08:00", "Home", "A")]
        self.assertEqual(self.guess(legs)["usualFare"], 1.0)              # else any recent paid trip
        legs = [leg("2025-01-01T17:30", "B", "C", fare=1.0), leg("2025-01-02T17:30", "A", "FAILED", transfer=True),
                leg("2025-01-03T08:00", "Home", "A")]
        self.assertEqual(self.guess(legs)["usualFare"], 0.0)              # a transfer leg is free


class StopNames(unittest.TestCase):
    def test_tokens(self):
        self.assertIn("38", build.norm_tokens("Gympie Rd f/s Hamilton Rd (St 38)"))
        # "(St N)" is stop N: it must not add a "street" that isn't in the name
        self.assertEqual(build.norm_tokens("Valley Mall (St 5)"), {"valley", "mall", "5"})
        self.assertIn("street", build.norm_tokens("Hamilton St"))
        toks = build.norm_tokens("Chermside Bus Station - Platform 2a")
        self.assertIn("p2a", toks)            # platform, not a stop number
        self.assertIn("busway", toks)
        self.assertIn("p1a", build.norm_tokens("Chermside bus station, 1a"))  # GTFS platform style
        self.assertIn("rbwh", build.norm_tokens("Royal Brisbane & Womens Hosp station"))
        self.assertNotIn("stop", build.norm_tokens("Main St stop 9"))

    def test_needs_a_shared_name_word(self):
        # "station" and platform numbers alone must not match an unrelated station
        sid, _ = build.guess_stop("Nowhere station platform 1", fakes.gtfs_dict())
        self.assertIsNone(sid)

    def test_cold_start_prefers_stops_near_the_others(self):
        gtfs = fakes.gtfs_dict()
        gtfs = {"FAR": gtfs.pop("FAR"), **gtfs}  # the far-away twin comes first, so ties would pick it
        stop_map = {}
        new = build.match_new_stops({"Alpha Road - 1", "Beta Street - 2", "Xville station"}, stop_map, gtfs)
        self.assertEqual(stop_map["Alpha Road - 1"]["stop_id"], "A")
        self.assertEqual(stop_map["Beta Street - 2"]["stop_id"], "B")
        self.assertEqual(stop_map["Xville station"]["stop_id"], "place_xsta")
        self.assertTrue(all(e["auto"] for e in stop_map.values()))
        self.assertEqual(len(new), 3)

    def test_known_stops_are_kept(self):
        stop_map = {"Alpha Road - 1": {"stop_id": "FAR"}}  # a hand correction, even if odd
        build.match_new_stops({"Alpha Road - 1", "Beta Street - 2"}, stop_map, fakes.gtfs_dict())
        self.assertEqual(stop_map["Alpha Road - 1"], {"stop_id": "FAR"})
        self.assertIn("Beta Street - 2", stop_map)

    def test_stop_modes(self):
        g = fakes.gtfs_dict()
        self.assertEqual(build.stop_mode(g["place_xsta"]), "train")
        self.assertEqual(build.stop_mode(g["X1"]), "train")          # platform of a train station
        self.assertEqual(build.stop_mode(g["F"]), "ferry")
        self.assertEqual(build.stop_mode(g["L"]), "tram")
        self.assertEqual(build.stop_mode(g["A"]), "bus")
        busway = {"stop_id": "1", "stop_name": "Roma Street bus station", "parent_station": "place_romsta"}
        self.assertEqual(build.stop_mode(busway), "bus")


class TimetableChecks(unittest.TestCase):
    def test_results_are_kept_after_the_timetable_moves_on(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "checks.json"
        leg = {"on": "2026-10-27T08:00:00", "from": "A", "to": "B", "route": "10"}
        build.keep_timetable_checks([dict(leg, schedDep="08:00", schedMins=15, vsTimetable=4)], path)
        later = [dict(leg), dict(leg, on="2026-10-28T08:00:00")]          # a new feed: no fresh comparison
        build.keep_timetable_checks(later, path)
        self.assertEqual(later[0]["vsTimetable"], 4)
        self.assertNotIn("vsTimetable", later[1])
        rerouted = [dict(leg, route="20")]                                # matched differently now: don't reuse
        build.keep_timetable_checks(rerouted, path)
        self.assertNotIn("vsTimetable", rerouted[0])


def history_csv(rows):
    return "\n".join([fakes.HEADER] + [" " + r + " " for r in rows]) + "\n"


class FullBuild(unittest.TestCase):
    """build.py's main() end to end: history.csv + feed -> data.js, with stop matching and routes."""

    ROWS = [
        "04/Mar/2025,08:00 AM,Alpha Road - 1,08:15 AM,Beta Street - 2,$ 0.50,,$ 9.50",
        "04/Mar/2025,08:20 AM,Beta Street - 2 (Touch on Transfer),08:40 AM,Alpha Road - 1,$ 0.00,,$ 9.50",
        "05/Mar/2025,06:59 AM,Xville station,07:12 AM,Yton station,$ 0.50,,$ 9.00",
        "08/Mar/2025,08:59 AM,Fern ferry terminal,09:20 AM,Gum ferry terminal,$ 0.50,,$ 8.50",
        "09/Mar/2025,09:00 AM,Alpha Road - 1,09:30 AM,Fern ferry terminal,$ 0.50,,$ 8.00",
        "10/Mar/2025,10:00 AM,Auto top up(Alpha Road - 1),,,,$ 20.00,$ 28.00",
        "11/Mar/2025,18:00 PM,Alpha Road - 1,Unknown,Failed to touch Off,$ 5.00,,$ 23.00",
    ]

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.zip, self.stops_txt = fakes.make_gtfs(self.dir / "gtfs")
        (self.dir / "history.csv").write_text(history_csv(self.ROWS))

    def build(self, *extra):
        argv = ["build.py", "--history", str(self.dir / "history.csv"), "--stops", str(self.dir / "stops.json"),
                "--gtfs", str(self.stops_txt), "--gtfs-zip", str(self.zip), "--out", str(self.dir / "data.js"),
                "--checks", str(self.dir / "checks.json"), *extra]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            build.main()
        text = (self.dir / "data.js").read_text()
        self.assertTrue(text.startswith("window.TRAVEL = ") and text.rstrip().endswith(";"))
        return json.loads(text[len("window.TRAVEL = "):].rstrip().rstrip(";")), out.getvalue()

    def test_data_file(self):
        data, out = self.build()
        legs = data["legs"]
        self.assertEqual(len(legs), 6)
        self.assertEqual(len({l["journey"] for l in legs}), 5)          # the transfer joins a journey
        self.assertEqual([l.get("route") for l in legs[:4]], ["10", None, "XVYT", "F9"])
        self.assertEqual([l["mode"] for l in legs[:4]], ["bus", "bus", "train", "ferry"])
        self.assertTrue(legs[4]["kmApprox"])                            # no service: straight line
        self.assertNotIn("path", legs[4])
        self.assertAlmostEqual(legs[0]["km"], 1.98, places=1)            # along the route path
        self.assertTrue(legs[5]["failedTouchOff"])
        self.assertEqual(data["topups"][0]["amount"], 20.0)
        self.assertEqual(data["stops"]["Xville station"]["mode"], "train")
        self.assertEqual(data["stops"]["Fern ferry terminal"]["mode"], "ferry")
        self.assertEqual(set(data["network"]), {"train", "ferry", "tram"})
        self.assertIn("Auto-matched new stops", out)
        self.assertFalse(data["demo"])                                  # your own build never shows the demo banner
        self.assertTrue((self.dir / "checks.json").exists())            # results kept in the test's own folder
        self.assertEqual(data["timetable"], ["2025-01-01", "2027-12-31"])

    def test_stop_map_written_once_then_reused(self):
        self.build()
        stop_map = json.loads((self.dir / "stops.json").read_text())
        self.assertEqual(stop_map["Alpha Road - 1"]["stop_id"], "A")
        self.assertTrue(stop_map["Alpha Road - 1"]["auto"])
        _, out = self.build()
        self.assertNotIn("Auto-matched", out)                           # nothing new the second time

    def test_without_the_full_feed_lines_stay_straight(self):
        data, out = self.build("--gtfs-zip", str(self.dir / "missing.zip"))
        self.assertEqual(data["paths"], {})
        self.assertIn("fetch_gtfs.py", out)

    def test_empty_history_explains_what_to_do(self):
        (self.dir / "history.csv").write_text(fakes.HEADER + "\n")
        with self.assertRaises(SystemExit) as e:
            self.build()
        self.assertIn("fetch_history.py", str(e.exception))


if __name__ == "__main__":
    unittest.main()
