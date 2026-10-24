#!/usr/bin/env python3
"""Turn the master travel history (data/history.csv) into dashboard/data.js.

- reads the master kept by fetch_history.py (overlapping downloads are already merged)
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

from history import Master

ROOT = pathlib.Path(__file__).resolve().parent.parent

TRANSFER_TAG = "(Touch on Transfer)"
# Balance events in the From column, e.g. "Auto top up(Stop)", "Top Up( Eagle Farm Depot )"
# (topped up with the driver; the row is also that trip), "Adjustment(Stop)", "Top up/Adjustment(Stop)".
BALANCE_RE = re.compile(r"^(auto top ?up|top ?up/adjustment|adjustment|top ?up)\s*\((.*)\)$", re.I)
MONEY_RE = re.compile(r"(-)?\$?\s*(-)?(\d[\d,]*\.?\d*)")


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


def load_gtfs(path):
    if not path.exists():
        sys.exit(f"Missing {path} - run scripts/fetch_gtfs.py first")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["stop_id"]: r for r in csv.DictReader(f)}


ABBREV = {"st": "street", "rd": "road", "fs": "farside", "f/s": "farside", "app": "approaching",
          "kgs": "king george square", "dr": "drive", "dve": "drive", "tce": "terrace", "stn": "station",
          "pk": "park", "hosp": "hospital"}
# go card labels that share no words with their GTFS name
ALIASES = {r"royal brisbane (&|and) womens hospital": "rbwh"}
STOPWORDS = {"stop", "near", "at", "the", "opp", "op", "ib", "ob"}


GENERIC = {"street", "road", "station", "busway", "bus", "ferry", "terminal", "drive", "terrace"}


def is_platform(tok):
    return re.fullmatch(r"p\d+[a-z]?", tok) is not None


def norm_tokens(name):
    s = name.lower().replace("f/s", " farside ").replace("'", "")
    s = re.sub(r"\(st (\d+\w?)\)", r"(stop \1)", s)  # "(St 144)" is stop 144, not a street
    s = re.sub(r"platform (\w+)", r"p\1", s)          # platform numbers aren't stop numbers
    s = re.sub(r"station, (\d+[a-z]?)$", r"station p\1", s)  # GTFS: "Queen Street station, 1a"
    s = s.replace("bus station", "busway")
    s = " ".join(ABBREV.get(t, t) for t in s.split())
    for pat, alias in ALIASES.items():
        s = re.sub(pat, alias, s)
    toks = re.findall(r"\bp\d+[a-z]?\b|[a-z]+|\d+[a-z]?", s)
    out = []
    for t in toks:
        out.extend(ABBREV.get(t, t).split())
    return set(out) - STOPWORDS


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
        shared = have & want
        if not {t for t in shared - GENERIC if not is_platform(t)}:
            continue  # need a shared name word, not just "station" or a platform number
        score = len(have & want) / len(have | want)
        if nums and nums & have:
            score += 0.5
        if any(is_platform(t) for t in shared):
            score += 0.3
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
    ap.add_argument("--history", type=pathlib.Path, default=ROOT / "data" / "history.csv",
                    help="master travel history from fetch_history.py (default: data/history.csv)")
    ap.add_argument("--stops", type=pathlib.Path, default=ROOT / "data" / "stops.json",
                    help="go card stop name -> GTFS stop_id map, created if missing (default: data/stops.json)")
    ap.add_argument("--gtfs", type=pathlib.Path, default=ROOT / "data" / "gtfs" / "stops.txt",
                    help="GTFS stops.txt (default: data/gtfs/stops.txt)")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "dashboard" / "data.js",
                    help="generated dashboard data file (default: dashboard/data.js)")
    args = ap.parse_args()

    master = Master(args.history)
    raw = list(master.rows.values())
    if not raw:
        sys.exit(f"No rows in {args.history} - run scripts/fetch_history.py "
                 f"(or fetch_history.py --import data/raw to use exports you already have)")
    gtfs = load_gtfs(args.gtfs)
    stop_map = json.loads(args.stops.read_text()) if args.stops.exists() else {}

    legs, topups = [], []
    for date_s, on_s, frm, off_s, to, fare_s, credit_s, bal_s in raw:
        day = dt.datetime.strptime(date_s, "%d/%b/%Y").date()
        on = parse_time(day, on_s)
        time_unknown = on is None  # e.g. a penalty fare with no touch-on recorded
        on = on or dt.datetime.combine(day, dt.time())
        balance = money(bal_s)
        m = BALANCE_RE.match(frm)
        if m:
            kind = m.group(1).lower().replace(" ", "")
            kind = "auto" if kind.startswith("auto") else "adjustment" if "adjustment" in kind else "manual"
            fare = money(fare_s) or 0.0
            topups.append({"t": on.isoformat(), "stop": m.group(2).strip(), "amount": money(credit_s),
                           "balance": round(balance + fare, 2) if balance is not None else None,
                           "kind": kind, "auto": kind == "auto"})
            if not to:
                continue
            frm = ""  # topped up with the driver: the boarding stop isn't recorded
        transfer = TRANSFER_TAG.lower() in frm.lower()
        frm = re.sub(re.escape(TRANSFER_TAG), "", frm, flags=re.I).strip() or None
        failed = to.lower().startswith("failed to touch")
        off = None if failed else parse_time(day, off_s)
        if off and off < on:
            off += dt.timedelta(days=1)
        legs.append({
            "on": on.isoformat(), "off": off.isoformat() if off else None,
            "from": frm, "to": None if failed else (to or None),
            "mins": round((off - on).total_seconds() / 60) if off and not time_unknown else None,
            "fare": money(fare_s) or 0.0, "balance": balance,
            "transfer": transfer, "failedTouchOff": failed, "timeUnknown": time_unknown,
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
    names = {l["from"] for l in legs if l["from"]} | {l["to"] for l in legs if l["to"]}
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
        "history": args.history.name, "rows": len(raw), "stops": stops, "legs": legs, "topups": topups,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("window.TRAVEL = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    print(f"{len(legs)} legs in {jid + 1} journeys, {len(topups)} top-ups, {len(stops)} stops "
          f"from {len(raw)} rows in {args.history.name} -> {args.out}")


if __name__ == "__main__":
    main()
