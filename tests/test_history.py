"""history.py edge cases (the main merge rules are in test_parsing.py)."""
import pathlib
import tempfile
import unittest

import fakes  # noqa: F401  (puts scripts/ on the path)
from history import HEADER, Master, keyed, parse_rows


class Rows(unittest.TestCase):
    def test_strips_pads_and_truncates(self):
        rows = parse_rows(["header", " 01/Jan/2026 , 08:00 AM ,A", "02/Jan/2026,1,2,3,4,5,6,7,8,9", ",,,", ""])
        self.assertEqual(rows[0], ["01/Jan/2026", "08:00 AM", "A", "", "", "", "", ""])
        self.assertEqual(len(rows[1]), 8)
        self.assertEqual(len(rows), 2)  # rows with no date are skipped

    def test_keys_count_repeats(self):
        r = ["01/Jan/2026", "08:00 AM", "A", "", "", "", "", ""]
        self.assertEqual([k for k, _ in keyed([r, r])], [("01/Jan/2026", "08:00 AM", "A", 1),
                                                       ("01/Jan/2026", "08:00 AM", "A", 2)])


class MasterFile(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "sub" / "history.csv"

    def test_missing_file_is_empty(self):
        m = Master(self.path)
        self.assertEqual(m.rows, {})
        self.assertIsNone(m.latest_date())

    def test_save_creates_folder_writes_header_no_temp_left(self):
        m = Master(self.path)
        m.merge([["01/Jan/2026", "08:00 AM", "A", "08:10 AM", "B", "$ 0.50", "", "$ 1.00"]])
        self.assertEqual(m.save(), 1)
        self.assertEqual(self.path.read_text().splitlines()[0], ",".join(HEADER))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["history.csv"])

    def test_chronological_with_unknown_times_first_in_the_day(self):
        m = Master(self.path)
        m.merge([["02/Jan/2026", "09:00 AM", "A", "", "", "", "", ""],
                 ["02/Jan/2026", "", "", "Unknown", "Failed to touch Off", "$ 5.00", "", ""],
                 ["01/Jan/2026", "17:00 PM", "B", "", "", "", "", ""],
                 ["02/Jan/2026", "08:00 AM", "C", "", "", "", "", ""]])
        m.save()
        order = [(r[0], r[2]) for r in Master(self.path).rows.values()]
        self.assertEqual(order, [("01/Jan/2026", "B"), ("02/Jan/2026", ""), ("02/Jan/2026", "C"), ("02/Jan/2026", "A")])

    def test_values_with_commas_survive(self):
        m = Master(self.path)
        m.merge([["01/Jan/2026", "08:00 AM", "Stop, with comma", "", "", "$ 1,000.00", "", ""]])
        m.save()
        self.assertEqual(next(iter(Master(self.path).rows.values()))[2], "Stop, with comma")


if __name__ == "__main__":
    unittest.main()
