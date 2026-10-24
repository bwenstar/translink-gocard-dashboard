#!/usr/bin/env python3
"""Generate the example data in examples/: a year of trips for a fictional commuter.

Every trip is a real timetabled service from Translink's GTFS feed (the tap-on is a
minute or two before its departure, the tap-off just after its arrival), so the demo
dashboard shows realistic routes, times and paths. The output uses the exact go card
CSV format (see docs/site-contract.json), including transfers, auto top-ups and failed
touch-offs. Seeded, so re-running gives the same file for the same feed.

The commuter lives near Kedron Brook busway station, works in the city, takes the train
some days, catches CityCats at weekends and has a monthly Gold Coast day out (train +
light rail). Nothing here is anyone's real travel history.

Usage: python3 scripts/make_example.py   (needs data/gtfs/SEQ_GTFS.zip from fetch_gtfs.py)
"""
import argparse
import bisect
import csv
import datetime as dt
import io
import json
import pathlib
import random
import zipfile
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent

# go card label -> GTFS station (parent) id. Labels are in go card's style.
STOPS = {
    "Kedron Brook station": "place_kedbwy",
    "Roma Street busway station": "place_rompl",
    "Cultural Centre station": "place_ccbs",
    "Chermside interchange": "place_intche",
    "UQ Lakes station": "place_uqlksa",
    "Nundah station": "place_nunsta",
    "Central station": "place_censta",
    "Helensvale station": "place_helsta",
    "Helensvale light rail station": "place_helsta",
    "Broadbeach South light rail station": "place_brsstn",
    "South Bank ferry terminal": "place_soutft",
    "New Farm Park ferry terminal": "place_newfft",
    "Riverside ferry terminal": "place_riveft",
}
# (from, to, GTFS route_type) - the mode keeps e.g. buses out of the train legs
LEGS = {
    "to_work": ("Kedron Brook station", "Roma Street busway station", 3),
    "from_work": ("Roma Street busway station", "Kedron Brook station", 3),
    "train_in": ("Nundah station", "Central station", 2),
    "train_out": ("Central station", "Nundah station", 2),
    "to_uq": ("Roma Street busway station", "UQ Lakes station", 3),
    "from_uq": ("UQ Lakes station", "Roma Street busway station", 3),
    "to_shops": ("Kedron Brook station", "Chermside interchange", 3),
    "from_shops": ("Chermside interchange", "Kedron Brook station", 3),
    "to_cc": ("Kedron Brook station", "Cultural Centre station", 3),
    "ferry_out": ("South Bank ferry terminal", "New Farm Park ferry terminal", 4),
    "ferry_back": ("New Farm Park ferry terminal", "Riverside ferry terminal", 4),
    "gc_train": ("Central station", "Helensvale station", 2),
    "gc_tram": ("Helensvale light rail station", "Broadbeach South light rail station", 0),
    "gc_tram_back": ("Broadbeach South light rail station", "Helensvale light rail station", 0),
    "gc_train_back": ("Helensvale station", "Central station", 2),
}
FARE, PENALTY, TOPUP, LOW = 0.50, 5.00, 20.00, 5.00


def table(zf, name):
    f = io.TextIOWrapper(zf.open(name), encoding="utf-8-sig")
    return csv.DictReader(f)


def timetable(zip_path):
    """{leg: {weekday: sorted [(dep_min, arr_min)]}} from real GTFS trips."""
    with zipfile.ZipFile(zip_path) as zf:
        children = defaultdict(set)
        for r in table(zf, "stops.txt"):
            if r.get("parent_station"):
                children[r["parent_station"]].add(r["stop_id"])
            children[r["stop_id"]].add(r["stop_id"])
        rtype = {r["route_id"]: int(r["route_type"]) for r in table(zf, "routes.txt")
                 if "rail replacement" not in r["route_long_name"].lower()}
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        mask = {r["service_id"]: [i for i, d in enumerate(days) if r[d] == "1"] for r in table(zf, "calendar.txt")}
        trips = {r["trip_id"]: (rtype[r["route_id"]], mask.get(r["service_id"], range(7)))
                 for r in table(zf, "trips.txt") if r["route_id"] in rtype}
        station_of = {sid: st for st in set(STOPS.values()) for sid in children[st]}
        calls = defaultdict(dict)  # trip -> station -> (seq, minute)
        for r in table(zf, "stop_times.txt"):
            st = station_of.get(r["stop_id"])
            if st and r["trip_id"] in trips:
                h, m, _ = r["departure_time"].split(":")
                calls[r["trip_id"]].setdefault(st, (int(r["stop_sequence"]), int(h) * 60 + int(m)))
    out = {}
    for leg, (a, b, mode) in LEGS.items():
        A, B = STOPS[a], STOPS[b]
        by_day = defaultdict(set)
        for tid, c in calls.items():
            if A in c and B in c and c[A][0] < c[B][0] and trips[tid][0] == mode:
                for d in trips[tid][1]:
                    by_day[d].add((c[A][1], c[B][1]))
        if not by_day:
            raise SystemExit(f"No timetabled {leg} service ({a} -> {b}) in this feed")
        out[leg] = {d: sorted(v) for d, v in by_day.items()}
    return out


class Card:
    def __init__(self, rng, tt):
        self.rng, self.tt, self.rows, self.balance = rng, tt, [], 20.00

    def ride(self, day, leg, after_min, transfer=False, fail=False):
        """Board the first service of `leg` departing at or after `after_min`. Returns arrival minute."""
        options = self.tt[leg].get(day.weekday()) or []
        i = bisect.bisect_left(options, (after_min, 0))
        if i >= len(options):
            return None
        dep, arr = options[i]
        on = dep - self.rng.choice([0, 1, 1, 2])
        off = arr + self.rng.choice([0, 1])
        a, b, _ = LEGS[leg]
        if self.balance < LOW:
            self.balance += TOPUP
            self.rows.append((day, on - 1, f"Auto top up({a})", None, "", None, TOPUP))
        fare = PENALTY if fail else 0.0 if transfer else FARE
        self.balance -= fare
        frm = f"{a} (Touch on Transfer)" if transfer else a
        self.rows.append((day, on, frm, None if fail else off, "Failed to touch Off" if fail else b, fare, None))
        return arr

    def csv(self):
        def t(m):
            m %= 1440
            return f"{m // 60:02d}:{m % 60:02d} {'PM' if m >= 720 else 'AM'}"
        out, bal = [" Date,Time,From,Time,To,Fare,Credit,Balance"], 20.00
        for day, on, frm, off, to, fare, credit in sorted(self.rows, key=lambda r: (r[0], r[1])):
            bal += (credit or 0) - (fare or 0)
            out.append(" " + ",".join([
                day.strftime("%d/%b/%Y"), t(on), frm, "Unknown" if to.startswith("Failed") else t(off) if off else "",
                to, f"$ {fare:.2f}" if fare is not None else "", f"$ {credit:.2f}" if credit else "", f"$ {bal:.2f}",
            ]) + " ")
        return "\n".join(out) + "\n"




def simulate(tt, start, end, seed):
    rng = random.Random(seed)
    card = Card(rng, tt)
    leave = [(dt.date(start.year, 12, 22), dt.date(start.year + 1, 1, 5)),  # Christmas break
             (dt.date(start.year + 1, 6, 30), dt.date(start.year + 1, 7, 6))]
    fails = 0
    day = start
    while day <= end:
        wd = day.weekday()
        on_leave = any(a <= day <= b for a, b in leave)
        if wd < 5 and not on_leave and rng.random() < 0.88:
            fail = fails < 3 and rng.random() < 0.006
            fails += fail
            if rng.random() < 0.25:  # train day
                card.ride(day, "train_in", rng.randint(7 * 60 + 5, 8 * 60 + 10))
                card.ride(day, "train_out", rng.randint(16 * 60 + 40, 17 * 60 + 50), fail=fail)
            else:
                card.ride(day, "to_work", rng.randint(7 * 60 + 15, 8 * 60 + 20))
                if wd == 2 and rng.random() < 0.6:  # Wednesday afternoons at UQ
                    arr = card.ride(day, "to_uq", rng.randint(13 * 60, 13 * 60 + 40))
                    if arr:
                        back = card.ride(day, "from_uq", arr + rng.randint(120, 200))
                        if back:  # change at Roma Street for the busway home
                            card.ride(day, "from_work", back + rng.randint(3, 9), transfer=True, fail=fail)
                else:
                    card.ride(day, "from_work", rng.randint(16 * 60 + 30, 18 * 60 + 15), fail=fail)
        elif wd == 5 and not on_leave:
            if day.day <= 7:  # first Saturday of the month: Gold Coast
                arr = card.ride(day, "gc_train", rng.randint(8 * 60 + 30, 9 * 60 + 30))
                if arr:
                    arr = card.ride(day, "gc_tram", arr + rng.randint(4, 12), transfer=True)
                    back = card.ride(day, "gc_tram_back", rng.randint(15 * 60, 16 * 60 + 30))
                    if back:
                        card.ride(day, "gc_train_back", back + rng.randint(3, 10), transfer=True)
            elif rng.random() < 0.55:  # bus to the city, CityCat along the river
                arr = card.ride(day, "to_cc", rng.randint(9 * 60 + 30, 11 * 60))
                if arr:
                    card.ride(day, "ferry_out", arr + rng.randint(8, 20), transfer=True)
                    card.ride(day, "ferry_back", rng.randint(13 * 60 + 30, 15 * 60 + 30))
                    card.ride(day, "from_work", rng.randint(15 * 60 + 40, 16 * 60 + 30))
        elif wd == 6 and rng.random() < 0.35:
            arr = card.ride(day, "to_shops", rng.randint(10 * 60, 12 * 60))
            if arr:
                card.ride(day, "from_shops", arr + rng.randint(60, 150))
        day += dt.timedelta(days=1)
    return card


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs-zip", type=pathlib.Path, default=ROOT / "data" / "gtfs" / "SEQ_GTFS.zip")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "examples")
    ap.add_argument("--start", default="2025-11-03")
    ap.add_argument("--end", default="2026-10-16")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if not args.gtfs_zip.exists():
        raise SystemExit(f"Missing {args.gtfs_zip} - run scripts/fetch_gtfs.py first")
    tt = timetable(args.gtfs_zip)
    card = simulate(tt, dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end), args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "history.csv").write_text(card.csv())
    stops = {"_comment": "Example stop map for examples/history.csv: go card label -> GTFS stop_id (station ids)."}
    stops.update({label: {"stop_id": sid} for label, sid in STOPS.items()})
    (args.out / "stops.json").write_text(json.dumps(stops, indent=2) + "\n")
    print(f"Wrote {len(card.rows)} rows to {args.out / 'history.csv'} and {args.out / 'stops.json'}")


if __name__ == "__main__":
    main()
