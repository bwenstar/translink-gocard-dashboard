"""The master travel history: one CSV holding every row ever downloaded.

data/history.csv uses the same columns as a go card export, so it opens in a
spreadsheet like any export. New downloads are merged in: rows are keyed by their
touch-on (date, time, from) plus an occurrence count for genuine repeats, so
overlapping windows never double count. A row that changed (a trip that was still
open in an earlier download) is updated in place.
"""
import csv
import datetime as dt
import pathlib
import re

HEADER = ["Date", "Time", "From", "Time", "To", "Fare", "Credit", "Balance"]


def parse_rows(lines):
    """Rows of go card CSV text (header first), stripped and padded to 8 columns."""
    reader = csv.reader(lines)
    next(reader, None)
    out = []
    for r in reader:
        r = ([c.strip() for c in r] + [""] * 8)[:8]
        if r[0]:
            out.append(r)
    return out


def read_export(path):
    """Rows of one go card CSV file (an export or the master)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        return parse_rows(f)


def keyed(rows):
    """[(key, row)] where key = (date, time, from, n) and n counts repeats of the same touch-on."""
    seen, out = {}, []
    for r in rows:
        k = (r[0], r[1], r[2])
        seen[k] = seen.get(k, 0) + 1
        out.append((k + (seen[k],), r))
    return out


def _sort_key(r):
    day = dt.datetime.strptime(r[0], "%d/%b/%Y").date()
    m = re.match(r"(\d{1,2}):(\d{2})", r[1])
    return day, (int(m.group(1)) * 60 + int(m.group(2))) if m else -1


class Master:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.rows = dict(keyed(read_export(self.path))) if self.path.exists() else {}

    def merge(self, rows):
        """Merge rows from one export. Returns (added, updated)."""
        added = updated = 0
        for k, r in keyed(rows):
            old = self.rows.get(k)
            if old is None:
                added += 1
            elif old != r and not (old[4] and not r[4]):  # never trade a completed trip for an open one
                updated += 1
            else:
                continue
            self.rows[k] = r
        return added, updated

    def latest_date(self):
        return max((_sort_key(r)[0] for r in self.rows.values()), default=None)

    def save(self):
        rows = sorted(self.rows.values(), key=_sort_key)  # stable: same-minute rows keep their order
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        tmp.replace(self.path)
        return len(rows)
