#!/usr/bin/env python3
"""Check the stops build.py guessed ("auto": true in data/stops.json) against the timetable.

When build.py routes a leg, it records which timetable stop the matched service actually
used near each of your stops (within 200 m). For each guessed stop this compares the guess
with those:

  confirmed   your rides' services used the guessed stop
  suggest     they consistently used a different stop: probably the right one
  check       no ride there could be matched to a service; check it by hand

Run build.py first (this reads dashboard/data.js). Dry run by default; --apply writes
data/stops.json (keeping a .bak copy). It clears the flag on confirmed stops, and switches
to a suggestion only when at least --min-legs rides agree on it. Anything else is left
flagged for you to look at.
"""
import argparse
import collections
import csv
import json
import math
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_data_js(path):
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def metres(a, b):
    return math.hypot((float(a["stop_lat"]) - float(b["stop_lat"])) * 111_000,
                      (float(a["stop_lon"]) - float(b["stop_lon"])) * 99_000)


def review(stop_map, legs, gtfs, min_share=0.8):
    """[(name, verdict, current id, suggested id or None, legs routed, share)] for every guessed stop."""
    used = collections.defaultdict(collections.Counter)
    for l in legs:
        if not l.get("path"):
            continue
        _, sa, sb = l["path"].split(":")
        if l.get("from"):
            used[l["from"]][sa] += 1
        if l.get("to"):
            used[l["to"]][sb] += 1
    out = []
    for name, entry in stop_map.items():
        if name.startswith("_") or not entry.get("auto"):
            continue
        cur, counts = entry["stop_id"], used.get(name, collections.Counter())
        n = sum(counts.values())
        if not n:
            out.append((name, "check", cur, None, 0, 0.0))
            continue
        # the guess may be a station (parent) whose platforms the services use
        children = {sid for sid, r in gtfs.items() if r.get("parent_station") == cur}
        on_guess = sum(c for sid, c in counts.items() if sid == cur or sid in children)
        top, top_n = counts.most_common(1)[0]
        if on_guess / n >= min_share:
            out.append((name, "confirmed", cur, None, n, on_guess / n))
        elif top_n / n >= min_share:
            out.append((name, "suggest", cur, top, n, top_n / n))
        else:
            out.append((name, "check", cur, None, n, top_n / n))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stops", type=pathlib.Path, default=ROOT / "data" / "stops.json")
    ap.add_argument("--data", type=pathlib.Path, default=ROOT / "dashboard" / "data.js")
    ap.add_argument("--gtfs", type=pathlib.Path, default=ROOT / "data" / "gtfs" / "stops.txt")
    ap.add_argument("--apply", action="store_true", help="write the confirmed and well-supported changes")
    ap.add_argument("--min-legs", type=int, default=2, help="rides needed before switching a stop (default 2)")
    args = ap.parse_args(argv)

    stop_map = json.loads(args.stops.read_text(encoding="utf-8"))
    with open(args.gtfs, newline="", encoding="utf-8-sig") as f:
        gtfs = {r["stop_id"]: r for r in csv.DictReader(f)}
    results = review(stop_map, load_data_js(args.data)["legs"], gtfs)
    if not results:
        print(f"No guessed stops in {args.stops}: nothing to review.")
        return 0

    name_of = lambda sid: gtfs[sid]["stop_name"] if sid in gtfs else "?"
    changed = 0
    for name, verdict, cur, new, n, share in results:
        rides = f"{n} routed ride{'s' if n != 1 else ''}"
        if verdict == "confirmed":
            print(f"confirmed  {name!r}\n           -> {cur} {name_of(cur)!r} ({rides}, {share:.0%} agree)")
            if args.apply:
                stop_map[name] = {"stop_id": cur}
                changed += 1
        elif verdict == "suggest":
            d = metres(gtfs[cur], gtfs[new]) if cur in gtfs and new in gtfs else float("nan")
            print(f"suggest    {name!r}\n           -> {new} {name_of(new)!r} instead of {cur} {name_of(cur)!r} "
                  f"({d:.0f} m apart; {rides}, {share:.0%} used it)")
            if args.apply and n >= args.min_legs:
                stop_map[name] = {"stop_id": new}
                changed += 1
            elif args.apply:
                print(f"           left flagged: fewer than {args.min_legs} rides")
        else:
            why = "no ride here matched a timetabled service" if not n else "services used different stops"
            print(f"check      {name!r}\n           -> still {cur} {name_of(cur)!r}: {why}; check it on a map")

    if args.apply and changed:
        shutil.copy2(args.stops, args.stops.with_suffix(".json.bak"))
        args.stops.write_text(json.dumps(stop_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nUpdated {changed} stop(s) in {args.stops} (previous copy: {args.stops.with_suffix('.json.bak').name}). "
              "Rebuild: python3 scripts/build.py")
    elif not args.apply:
        print("\nDry run. Re-run with --apply to save the confirmed stops and suggestions backed by "
              f"{args.min_legs}+ rides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
