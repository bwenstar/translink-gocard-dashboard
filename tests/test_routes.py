"""routes.py: picking the service a leg most likely took, cutting its path, caching, the network layer."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import fakes
import routes

NAMES = {  # go card label -> GTFS stop id, as stops.json would map them
    "Alpha Road - 1": "A", "Beta Street - 2": "B",
    "Xville station": "place_xsta", "Yton station": "place_ysta",
    "Fern ferry terminal": "F", "Gum ferry terminal": "G",
}
EXPECT = {"Xville station": "train", "Yton station": "train"}
TUE, SAT = "2025-03-04", "2025-03-08"


class Infer(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.zip, _ = fakes.make_gtfs(self.dir)
        self.gtfs = fakes.gtfs_dict()
        self.cache = self.dir / "paths_cache.json"

    def infer(self, legs, expect=EXPECT):
        return routes.infer(legs, NAMES, self.gtfs, self.zip, self.cache, expect)

    def test_nearest_departure_on_that_weekday(self):
        legs = [fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T07:59"),
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:28"),
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{SAT}T08:09"),
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:09")]
        self.infer(legs)
        # route 20 (08:10) only runs at weekends: on a Tuesday at 08:09 it must not be picked
        self.assertEqual([l["route"] for l in legs], ["10", "10", "20", "10"])
        self.assertEqual({l["mode"] for l in legs}, {"bus"})
        self.assertEqual(legs[0]["routeName"], "Testville - City")

    def test_path_is_cut_between_the_stops(self):
        legs = [fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00")]
        paths, stats = self.infer(legs)
        pts = paths[legs[0]["path"]]
        self.assertEqual(pts[0], [-27.4, 153.0])     # starts at A, not at the shape's start (152.99)
        self.assertEqual(pts[-1], [-27.4, 153.02])   # ends at B, not the shape's end (153.03)
        self.assertTrue(all(153.0 <= p[1] <= 153.02 for p in pts))
        self.assertEqual(stats["legs"], 1)

    def test_train_beats_a_bus_between_two_stations(self):
        # bus 30 leaves 1 minute after tap-on from a stop 44 m away; the train left 3 minutes before
        legs = [fakes.leg("Xville station", "Yton station", f"{TUE}T07:03")]
        self.infer(legs, expect={})
        self.assertEqual(legs[0]["route"], "30")      # on timing alone the bus would win...
        legs = [fakes.leg("Xville station", "Yton station", f"{TUE}T07:03")]
        self.infer(legs)
        self.assertEqual((legs[0]["route"], legs[0]["mode"]), ("XVYT", "train"))  # ...but these are stations

    def test_rail_replacement_buses_are_ignored(self):
        # at 07:06 the rail replacement bus (07:05) is the closest departure of all
        legs = [fakes.leg("Xville station", "Yton station", f"{TUE}T07:06")]
        self.infer(legs, expect={})
        self.assertNotEqual(legs[0]["route"], "R99")

    def test_no_service_means_no_path(self):
        legs = [fakes.leg("Alpha Road - 1", "Fern ferry terminal", f"{TUE}T08:00"),
                fakes.leg("Alpha Road - 1", None, f"{TUE}T08:00"),       # failed touch-off
                fakes.leg(None, "Beta Street - 2", f"{TUE}T08:00")]      # driver top-up, no boarding stop
        paths, stats = self.infer(legs)
        self.assertEqual(paths, {})
        self.assertTrue(all("path" not in l and "route" not in l for l in legs))
        self.assertEqual(stats["unmatched"], ["Alpha Road - 1 → Fern ferry terminal"])

    def test_unknown_time_still_gets_a_route(self):
        legs = [fakes.leg("Fern ferry terminal", "Gum ferry terminal", f"{SAT}T00:00", timeUnknown=True)]
        self.infer(legs)
        self.assertEqual((legs[0]["route"], legs[0]["mode"]), ("F9", "ferry"))

    def test_second_run_uses_the_cache(self):
        legs = [fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00")]
        first, _ = self.infer(legs)
        again = [fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00")]
        with mock.patch.object(routes, "_find_options", side_effect=AssertionError("re-scanned stop_times")), \
                mock.patch.object(routes, "_load_shapes", side_effect=AssertionError("re-read shapes")):
            second, _ = self.infer(again)
        self.assertEqual(first, second)
        self.assertEqual(again[0]["route"], "10")

    def test_cache_is_dropped_when_the_feed_changes(self):
        self.infer([fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00")])
        cache = json.loads(self.cache.read_text())
        cache["feed"] = "old-feed"
        self.cache.write_text(json.dumps(cache))
        with mock.patch.object(routes, "_find_options", wraps=routes._find_options) as spy:
            self.infer([fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00")])
        spy.assert_called_once()

    def test_ride_time_vs_timetable(self):
        legs = [fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T07:59", mins=21),   # timetabled 15
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:29", mins=12),
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00", mins=None),  # failed touch-off
                fakes.leg("Alpha Road - 1", "Beta Street - 2", f"{TUE}T08:00", mins=15, timeUnknown=True)]
        _, stats = self.infer(legs)
        self.assertEqual((legs[0]["schedDep"], legs[0]["schedMins"], legs[0]["vsTimetable"]), ("08:00", 15, 6))
        self.assertEqual(legs[1]["vsTimetable"], -3)
        self.assertTrue(all("vsTimetable" not in l for l in legs[2:]))
        self.assertEqual(stats["feed"], ["2025-01-01", "2027-12-31"])   # no feed_info.txt: from calendar.txt

    def test_rides_outside_the_timetable_period_are_not_compared(self):
        old = [fakes.leg("Alpha Road - 1", "Beta Street - 2", "2024-03-05T08:00", mins=20)]
        self.infer(old)
        self.assertEqual(old[0]["route"], "10")                 # still routed, just not compared
        self.assertNotIn("vsTimetable", old[0])
        demo = [fakes.leg("Alpha Road - 1", "Beta Street - 2", "2024-03-05T08:00", mins=20)]
        routes.infer(demo, NAMES, self.gtfs, self.zip, self.cache, EXPECT, compare_all=True)
        self.assertEqual(demo[0]["vsTimetable"], 5)

    def test_only_compared_when_the_timetable_stops_are_yours(self):
        # the station shuttle (bus 30) uses stops 44 m from each station: 88 m off in total, within 100
        legs = [fakes.leg("Xville station", "Yton station", f"{TUE}T07:03", mins=22)]
        self.infer(legs, expect={})
        self.assertEqual((legs[0]["route"], legs[0]["schedMins"]), ("30", 21))
        with mock.patch.object(routes, "COMPARE_M", 50):
            legs = [fakes.leg("Xville station", "Yton station", f"{TUE}T07:03", mins=22)]
            self.infer(legs, expect={})
        self.assertNotIn("vsTimetable", legs[0])

    def test_network_has_rail_ferry_and_light_rail_only(self):
        net = routes.network(self.zip, self.cache)
        self.assertEqual(set(net), {"train", "ferry", "tram"})
        self.assertEqual([l["route"] for l in net["train"]], ["XVYT"])
        self.assertEqual(net["tram"][0]["name"], "Lake line")


class Geometry(unittest.TestCase):
    def test_minutes_past_midnight(self):
        self.assertEqual(routes._minutes("07:05:00"), 425)
        self.assertEqual(routes._minutes("25:10:00"), 1510)  # GTFS times run past 24h for late trips

    def test_simplify_drops_straight_points_keeps_corners(self):
        line = [(-27.4, 153.0 + i * 0.001) for i in range(10)]
        self.assertEqual(routes._simplify(line), [line[0], line[-1]])
        corner = [(-27.4, 153.0), (-27.4, 153.01), (-27.39, 153.01)]
        self.assertEqual(routes._simplify(corner), corner)

    def test_cut_ends_exactly_on_the_stops(self):
        shape = fakes.SHAPES["SH10"]
        seg = routes._cut(shape, (-27.4, 153.005), (-27.4, 153.015))
        self.assertEqual(seg[0], [-27.4, 153.005])
        self.assertEqual(seg[-1], [-27.4, 153.015])

    def test_metres(self):
        self.assertAlmostEqual(routes.metres((-27.4, 153.0), (-27.4, 153.01)), 990, delta=1)


if __name__ == "__main__":
    unittest.main()
