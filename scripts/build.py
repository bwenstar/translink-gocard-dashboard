#!/usr/bin/env python3
"""Merge every go card CSV export under data/raw/ into dashboard/data.js.

- de-duplicates rows across overlapping exports
- groups transfer legs into journeys
- geocodes stops via data/stops.json -> GTFS stops.txt, auto-guessing new stops
"""
import argparse
import csv
import datetime as dt
import difflib
import json
import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

TRANSFER_TAG = "(Touch on Transfer)"
TOPUP_RE = re.compile(r"^(auto )?top ?up\s*\((.*)\)$", re.I)
MONEY_RE = re.compile(r"(-)?\$\s*(-)?([\d,]+\.?\d*)")


def money(s):
    m = MONEY_RE.search(s or "")
    if not m:
        return None
    v = float(m.group(3).replace(",", ""))
    return -v if (m.group(1) or m.group(2)) else v


def parse_time(day, s):
    """go card prints 24h times with a redundant AM/PM suffix, e.g. '17:20 PM'."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", (s or "").strip(), re.I)
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ampm == "PM" and h < 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return dt.datetime.combine(day, dt.time(h, mi))


def read_raw(raw_dir):
    """Every *.csv under raw_dir, at any depth and with any folder naming."""
    rows, sources = {}, []
    for path in sorted(raw_dir.rglob("*.csv")):
        sources.append(str(path.relative_to(raw_dir)))
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # header: Date,Time,From,Time,To,Fare,Credit,Balance
            for r in reader:
                r = [c.strip() for c in r] + [""] * 8
                if not r[0]:
                    continue
                key = tuple(r[:8])
                rows[key] = r[:8]
    return list(rows.values()), sources


def load_gtfs(path):
    if not path.exists():
        sys.exit(f"Missing {path} - run scripts/fetch_gtfs.py first")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["stop_id"]: r for r in csv.DictReader(f)}


ABBREV = {"st": "street", "rd": "road", "fs": "farside", "f/s": "farside", "app": "approaching",
          "kgs": "king george square", "dr": "drive", "tce": "terrace", "mt": "mt", "stn": "station"}


def norm_tokens(name):
    s = name.lower().replace("f/s", " farside ").replace("'", "")
    toks = re.findall(r"[a-z]+|\d+[a-z]?", s)
    out = []
    for t in toks:
        out.extend(ABBREV.get(t, t).split())
    return set(out) - {"stop", "near", "at", "platform", "the"}


def km(a, b):
    return math.hypot((a[0] - b[0]) * 111, (a[1] - b[1]) * 99)


def guess_stop(name, gtfs, known=()):
    """Best-effort fuzzy match of a go card stop label against GTFS stop names.

    Candidates far from `known` points (stops you already use) are penalised, since SEQ has
    many same-named streets.
    """
    want = norm_tokens(name)
    nums = {t for t in want if t[0].isdigit()}
    best, best_score = None, 0.0
    for sid, r in gtfs.items():
        have = norm_tokens(r["stop_name"])
        if not have & want:
            continue
        score = len(have & want) / len(have | want)
        if nums and nums & have:
            score += 0.5
        score += 0.2 * difflib.SequenceMatcher(None, name.lower(), r["stop_name"].lower()).ratio()
        if known:
            here = (float(r["stop_lat"]), float(r["stop_lon"]))
            score -= min(1.0, 0.02 * min(km(here, k) for k in known))
        if score > best_score:
            best, best_score = sid, score
    return best, round(best_score, 2)


def coords(r):
    return float(r["stop_lat"]), float(r["stop_lon"])


def match_new_stops(names, stop_map, gtfs):
    """Add a guessed entry to stop_map for each name it lacks. Returns [(name, gtfs_name, score)]."""
    todo = sorted(n for n in names if n not in stop_map)
    if not todo:
        return []
    known = [coords(gtfs[e["stop_id"]]) for k, e in stop_map.items()
             if not k.startswith("_") and e.get("stop_id") in gtfs]
    if not known:
        # Cold start: a first pass without a location prior, then anchor on the median of
        # those guesses (most of anyone's stops cluster in one area) and match again.
        first = [coords(gtfs[sid]) for sid, _ in (guess_stop(n, gtfs) for n in todo) if sid]
        if first:
            known = [(sorted(p[0] for p in first)[len(first) // 2],
                      sorted(p[1] for p in first)[len(first) // 2])]
    new = []
    for n in todo:
        sid, score = guess_stop(n, gtfs, known)
        if sid:
            stop_map[n] = {"stop_id": sid, "auto": True, "score": score}
            new.append((n, gtfs[sid]["stop_name"], score))
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=pathlib.Path, default=ROOT / "data" / "raw",
                    help="folder searched recursively for go card CSV exports (default: data/raw)")
    ap.add_argument("--stops", type=pathlib.Path, default=ROOT / "data" / "stops.json",
                    help="go card stop name -> GTFS stop_id map, created if missing (default: data/stops.json)")
    ap.add_argument("--gtfs", type=pathlib.Path, default=ROOT / "data" / "gtfs" / "stops.txt",
                    help="GTFS stops.txt (default: data/gtfs/stops.txt)")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "dashboard" / "data.js",
                    help="generated dashboard data file (default: dashboard/data.js)")
    args = ap.parse_args()

    raw, sources = read_raw(args.raw)
    if not raw:
        sys.exit(f"No CSV rows found under {args.raw}")
    gtfs = load_gtfs(args.gtfs)
    stop_map = json.loads(args.stops.read_text()) if args.stops.exists() else {}

    legs, topups = [], []
    for date_s, on_s, frm, off_s, to, fare_s, credit_s, bal_s in raw:
        day = dt.datetime.strptime(date_s, "%d/%b/%Y").date()
        on = parse_time(day, on_s)
        m = TOPUP_RE.match(frm)
        if m:
            topups.append({"t": on.isoformat(), "stop": m.group(2).strip(), "amount": money(credit_s),
                           "balance": money(bal_s), "auto": bool(m.group(1))})
            continue
        transfer = TRANSFER_TAG.lower() in frm.lower()
        frm = re.sub(re.escape(TRANSFER_TAG), "", frm, flags=re.I).strip()
        failed = to.lower().startswith("failed to touch")
        off = None if failed else parse_time(day, off_s)
        if off and off < on:
            off += dt.timedelta(days=1)
        legs.append({
            "on": on.isoformat(), "off": off.isoformat() if off else None,
            "from": frm, "to": None if failed else to,
            "mins": round((off - on).total_seconds() / 60) if off else None,
            "fare": money(fare_s) or 0.0, "balance": money(bal_s),
            "transfer": transfer, "failedTouchOff": failed,
        })

    legs.sort(key=lambda l: l["on"])
    topups.sort(key=lambda t: t["t"])

    # A transfer leg continues the journey of the leg before it.
    jid = -1
    for i, leg in enumerate(legs):
        if not (leg["transfer"] and i > 0):
            jid += 1
        leg["journey"] = jid

    # Geocode every stop name we've seen; guess any new ones.
    names = {l["from"] for l in legs} | {l["to"] for l in legs if l["to"]} | {t["stop"] for t in topups}
    new = match_new_stops(names, stop_map, gtfs)
    if new:
        args.stops.parent.mkdir(parents=True, exist_ok=True)
        args.stops.write_text(json.dumps(stop_map, indent=2, ensure_ascii=False) + "\n")
        print(f"Auto-matched new stops (please check {args.stops}):")
        for n, g, s in new:
            print(f"  {n!r:45} -> {g!r} (score {s})")

    stops, missing = {}, []
    for n in sorted(names):
        entry = stop_map.get(n)
        g = gtfs.get(entry["stop_id"]) if entry else None
        if not g:
            missing.append(n)
            continue
        stops[n] = {"lat": float(g["stop_lat"]), "lon": float(g["stop_lon"]), "stop_id": g["stop_id"],
                    "gtfs_name": g["stop_name"], "auto": bool(entry.get("auto"))}
    if missing:
        print("No coordinates for:", ", ".join(missing))

    payload = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "sources": sources, "stops": stops, "legs": legs, "topups": topups,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("window.TRAVEL = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    print(f"{len(legs)} legs in {jid + 1} journeys, {len(topups)} top-ups, {len(stops)} stops "
          f"from {len(sources)} file(s) -> {args.out}")


if __name__ == "__main__":
    main()
