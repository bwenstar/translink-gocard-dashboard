"""review_stops.py: guessed stops are confirmed, corrected or left for a human, using the timetable."""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

import fakes
import review_stops

GTFS = fakes.gtfs_dict()


def leg(frm, to, sa, sb):
    return {"from": frm, "to": to, "path": f"SH:{sa}:{sb}"}


class Review(unittest.TestCase):
    def test_verdicts(self):
        stop_map = {
            "Alpha Road - 1": {"stop_id": "A", "auto": True},          # services used A: confirmed
            "Beta Street - 2": {"stop_id": "FAR", "auto": True},       # services used B: suggest B
            "Xville station": {"stop_id": "place_xsta", "auto": True}, # services used its platform X1
            "Fern ferry terminal": {"stop_id": "F", "auto": True},     # never routed: check by hand
            "Gum ferry terminal": {"stop_id": "G"},                     # already checked: ignored
        }
        legs = [leg("Alpha Road - 1", "Beta Street - 2", "A", "B")] * 3 + [leg("Xville station", "Gum ferry terminal", "X1", "G")]
        got = {name: (verdict, new) for name, verdict, _, new, _, _ in review_stops.review(stop_map, legs, GTFS)}
        self.assertEqual(got, {"Alpha Road - 1": ("confirmed", None), "Beta Street - 2": ("suggest", "B"),
                               "Xville station": ("confirmed", None), "Fern ferry terminal": ("check", None)})

    def test_mixed_evidence_is_left_for_a_human(self):
        stop_map = {"Alpha Road - 1": {"stop_id": "FAR", "auto": True}}
        legs = [leg("Alpha Road - 1", "x", "A", "B"), leg("Alpha Road - 1", "x", "XB", "B")]
        self.assertEqual(review_stops.review(stop_map, legs, GTFS)[0][1], "check")


class Apply(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.stops = self.dir / "stops.json"
        self.stops.write_text(json.dumps({
            "_comment": "x",
            "Alpha Road - 1": {"stop_id": "A", "auto": True, "score": 0.9},
            "Beta Street - 2": {"stop_id": "FAR", "auto": True, "score": 0.5},
            "Fern ferry terminal": {"stop_id": "F", "auto": True},
        }))
        _, stops_txt = fakes.make_gtfs(self.dir / "gtfs")
        self.gtfs = stops_txt

    def run_review(self, legs, *extra):
        data = self.dir / "data.js"
        data.write_text("window.TRAVEL = " + json.dumps({"legs": legs}) + ";\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            review_stops.main(["--stops", str(self.stops), "--data", str(data), "--gtfs", str(self.gtfs), *extra])
        return json.loads(self.stops.read_text()), out.getvalue()

    def test_dry_run_changes_nothing(self):
        before = self.stops.read_text()
        _, out = self.run_review([leg("Alpha Road - 1", "Beta Street - 2", "A", "B")] * 2)
        self.assertEqual(self.stops.read_text(), before)
        self.assertIn("Dry run", out)

    def test_apply(self):
        m, _ = self.run_review([leg("Alpha Road - 1", "Beta Street - 2", "A", "B")] * 2, "--apply")
        self.assertEqual(m["Alpha Road - 1"], {"stop_id": "A"})            # confirmed: flag cleared
        self.assertEqual(m["Beta Street - 2"], {"stop_id": "B"})           # switched to what services used
        self.assertTrue(m["Fern ferry terminal"]["auto"])                  # unknown: still flagged
        self.assertEqual(m["_comment"], "x")
        self.assertTrue((self.dir / "stops.json.bak").exists())

    def test_one_ride_is_not_enough_to_switch(self):
        m, out = self.run_review([leg("Alpha Road - 1", "Beta Street - 2", "A", "B")], "--apply")
        self.assertEqual(m["Beta Street - 2"]["stop_id"], "FAR")
        self.assertIn("fewer than 2 rides", out)


if __name__ == "__main__":
    unittest.main()
