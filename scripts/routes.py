"""Infer the route each leg most likely took and the path it drew on the map.

For every (from, to) stop pair, the GTFS timetable is searched for trips that call at
the tap-on stop and later at the tap-off stop. Stops within NEAR_M of the mapped stop
also count, which covers station platforms and stops across the road. Each leg then
takes the option whose departure is nearest its tap-on time on the same weekday. The
trip's shape (shapes.txt) is cut between the two stops and simplified.

The timetable is the current one, so routes for older trips are a best guess.
Results are cached in data/gtfs/paths_cache.json, keyed on the feed file, so only
new stop pairs cost a pass over stop_times.txt.
"""
import csv
import datetime as dt
import io
import json
import math
import zipfile
from collections import defaultdict

NEAR_M = 200
CACHE_VERSION = 3
MODES = {0: "tram", 2: "train", 3: "bus", 4: "ferry"}


def metres(a, b):
    return math.hypot((a[0] - b[0]) * 111_000, (a[1] - b[1]) * 99_000)


def _rows(zf, name):
    with zf.open(name) as f:
        reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        header = next(reader)
        yield {h: i for i, h in enumerate(header)}
        yield from reader


def _table(zf, name):
    it = _rows(zf, name)
    return next(it), it


def _minutes(hms):
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)  # may exceed 24h for after-midnight trips


def _simplify(pts, tol=8.0):
    """Douglas-Peucker in metres."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = pts[i], pts[j]
        ax, ay, bx, by = a[1] * 99_000, a[0] * 111_000, b[1] * 99_000, b[0] * 111_000
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-9
        far, far_d = None, tol
        for k in range(i + 1, j):
            px, py = pts[k][1] * 99_000, pts[k][0] * 111_000
            t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > far_d:
                far, far_d = k, d
        if far is not None:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(pts, keep) if k]


def _cut(shape, a, b):
    """Shape points from the point nearest a to the next nearest point to b."""
    ia = min(range(len(shape)), key=lambda i: metres(shape[i], a))
    rest = range(ia, len(shape))
    ib = min(rest, key=lambda i: metres(shape[i], b))
    seg = shape[ia:ib + 1]
    return [list(a)] + [[round(p[0], 5), round(p[1], 5)] for p in _simplify(seg)] + [list(b)]


def _find_options(zf, pairs, near, skip_routes):
    """{pair: [[route_id, shape_id, stopA, stopB, penalty_m, [[minute, weekday_mask], ...]], ...]}"""
    want = defaultdict(list)  # gtfs stop_id -> [(name, distance)]
    for name, cands in near.items():
        for sid, d in cands:
            want[sid].append((name, d))
    names_needed = {n for p in pairs for n in p}

    h, it = _table(zf, "calendar.txt")
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    masks = {r[h["service_id"]]: sum(1 << i for i, d in enumerate(days) if r[h[d]] == "1") for r in it}
    h, it = _table(zf, "trips.txt")
    trips = {r[h["trip_id"]]: (r[h["route_id"]], r[h["shape_id"]], masks.get(r[h["service_id"]], 127)) for r in it
             if r[h["route_id"]] not in skip_routes}

    # trip -> name -> (seq, stop_id, minute, distance), keeping the closest stop per name
    calls = defaultdict(dict)
    h, it = _table(zf, "stop_times.txt")
    ti, si, sq, dep = h["trip_id"], h["stop_id"], h["stop_sequence"], h["departure_time"]
    for r in it:
        hits = want.get(r[si])
        if not hits:
            continue
        c = calls[r[ti]]
        for name, d in hits:
            if name in names_needed and (name not in c or d < c[name][3]):
                c[name] = (int(r[sq]), r[si], _minutes(r[dep]), d)

    groups = defaultdict(lambda: defaultdict(set))
    for trip_id, c in calls.items():
        if len(c) < 2 or trip_id not in trips:
            continue
        route_id, shape_id, mask = trips[trip_id]
        for a in c:
            for b in c:
                if a != b and (a, b) in pairs and c[a][0] < c[b][0]:
                    A, B = c[a], c[b]
                    groups[(a, b)][(route_id, shape_id, A[1], B[1], round(A[3] + B[3]))].add((A[2], mask))
    return {p: [list(k) + [sorted(v)] for k, v in g.items()] for p, g in groups.items()}


def _load_shapes(zf, shape_ids):
    pts = defaultdict(list)
    h, it = _table(zf, "shapes.txt")
    i, la, lo, sq = h["shape_id"], h["shape_pt_lat"], h["shape_pt_lon"], h["shape_pt_sequence"]
    for r in it:
        if r[i] in shape_ids:
            pts[r[i]].append((int(r[sq]), float(r[la]), float(r[lo])))
    return {k: [(a, b) for _, a, b in sorted(v)] for k, v in pts.items()}


def infer(legs, name_to_stop, gtfs, zip_path, cache_path, expect=None):
    """Adds "route" (short name), "mode" and "path" (a key into the returned paths) to legs.

    name_to_stop: go card name -> GTFS stop_id. expect: go card name -> the mode its stop
    looks like (train station, ferry terminal...); options of another mode are penalised.
    Returns ({path_key: [[lat, lon], ...]}, stats).
    """
    expect = expect or {}
    zsig = f"{zip_path.stat().st_size}:{int(zip_path.stat().st_mtime)}"
    try:
        cache = json.loads(cache_path.read_text())
        if cache.get("feed") != zsig or cache.get("v") != CACHE_VERSION:
            cache = None
    except (OSError, ValueError):
        cache = None
    cache = cache or {"v": CACHE_VERSION, "feed": zsig, "pairs": {}, "paths": {}}

    def pkey(a, b):
        return f"{a}\t{name_to_stop.get(a)}\t{b}\t{name_to_stop.get(b)}"

    pairs = {(l["from"], l["to"]) for l in legs
             if l["from"] in name_to_stop and l["to"] in name_to_stop and l["from"] != l["to"]}
    todo = {p for p in pairs if pkey(*p) not in cache["pairs"]}

    with zipfile.ZipFile(zip_path) as zf:
        h, it = _table(zf, "routes.txt")
        routes = {r[h["route_id"]]: (r[h["route_short_name"]], int(r[h["route_type"]]), r[h["route_long_name"]])
                  for r in it}
        if todo:
            platforms = [(sid, (float(r["stop_lat"]), float(r["stop_lon"]))) for sid, r in gtfs.items()
                         if r.get("location_type", "0") in ("", "0")]
            names = {n for p in todo for n in p}
            near = {}
            for n in names:
                g = gtfs[name_to_stop[n]]
                here = (float(g["stop_lat"]), float(g["stop_lon"]))
                near[n] = [(sid, metres(here, ll)) for sid, ll in platforms
                           if abs(ll[0] - here[0]) < 0.003 and metres(here, ll) <= NEAR_M]
            # temporary rail replacement buses would otherwise "explain" train trips
            skip = {rid for rid, (_, _, name) in routes.items() if "rail replacement" in name.lower()}
            found = _find_options(zf, todo, near, skip)
            for p in todo:
                cache["pairs"][pkey(*p)] = found.get(p, [])

        # choose an option per leg
        chosen = []
        for l in legs:
            opts = cache["pairs"].get(pkey(l["from"], l["to"])) if (l["from"], l["to"]) in pairs else None
            if not opts:
                chosen.append(None)
                continue
            on = dt.datetime.fromisoformat(l["on"])
            bit, minute = 1 << on.weekday(), on.hour * 60 + on.minute
            best, best_s = None, math.inf
            want = {expect.get(l["from"]), expect.get(l["to"])} - {None, "bus"}
            for route_id, shape_id, sa, sb, pen, deps in opts:
                same_day = [m for m, mask in deps if mask & bit] or [m for m, _ in deps]
                gap = min(min(abs(m - minute), abs(m - 1440 - minute)) for m in same_day)
                s = gap if not l["timeUnknown"] else 0
                s += pen / 50  # 50 m from the mapped stop ~ 1 minute of timetable gap
                if want and MODES.get(routes.get(route_id, ("", 3, ""))[1]) not in want:
                    s += 120  # a bus between two train stations only if nothing else fits
                if s < best_s:
                    best, best_s = (route_id, shape_id, sa, sb), s
            chosen.append(best)

        need = {f"{sh}:{sa}:{sb}" for c in chosen if c for _, sh, sa, sb in [c]} - cache["paths"].keys()
        if need:
            shapes = _load_shapes(zf, {k.split(":")[0] for k in need})
            for k in need:
                sh, sa, sb = k.split(":")
                if sh in shapes and sa in gtfs and sb in gtfs:
                    a = (float(gtfs[sa]["stop_lat"]), float(gtfs[sa]["stop_lon"]))
                    b = (float(gtfs[sb]["stop_lat"]), float(gtfs[sb]["stop_lon"]))
                    cache["paths"][k] = _cut(shapes[sh], a, b)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache))

    paths, routed = {}, 0
    for l, c in zip(legs, chosen):
        if not c:
            continue
        route_id, sh, sa, sb = c
        short, rtype, long_name = routes.get(route_id, ("", 3, ""))
        l["route"], l["routeName"], l["mode"] = short or None, long_name or None, MODES.get(rtype, "bus")
        k = f"{sh}:{sa}:{sb}"
        if k in cache["paths"]:
            l["path"] = k
            paths[k] = cache["paths"][k]
            routed += 1
    return paths, {"legs": routed, "pairs": len(pairs),
                   "unmatched": sorted(f"{a} → {b}" for a, b in pairs if not cache["pairs"].get(pkey(a, b)))}


def network(zip_path, cache_path):
    """Rail, light rail and ferry lines for the map's network layer: {mode: [{route, name, pts}]}.

    One shape per route (the one most trips use), simplified to ~25 m. Cached with the paths.
    """
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    zsig = f"{zip_path.stat().st_size}:{int(zip_path.stat().st_mtime)}"
    if cache.get("feed") == zsig and cache.get("v") == CACHE_VERSION and "network" in cache:
        return cache["network"]
    with zipfile.ZipFile(zip_path) as zf:
        h, it = _table(zf, "routes.txt")
        lines = {}
        for r in it:
            mode = MODES.get(int(r[h["route_type"]]))
            if mode in ("train", "ferry", "tram"):
                lines[r[h["route_id"]]] = (mode, r[h["route_short_name"]], r[h["route_long_name"]])
        counts = defaultdict(lambda: defaultdict(int))  # (mode, short) -> shape -> trips
        names = {}
        h, it = _table(zf, "trips.txt")
        for r in it:
            info = lines.get(r[h["route_id"]])
            if info and r[h["shape_id"]]:
                counts[info[:2]][r[h["shape_id"]]] += 1
                names[info[:2]] = info[2]
        best = {k: max(v, key=v.get) for k, v in counts.items()}
        shapes = _load_shapes(zf, set(best.values()))
    out, seen = defaultdict(list), set()
    for (mode, short), sh in sorted(best.items()):
        if sh in seen or sh not in shapes:
            continue
        seen.add(sh)
        pts = [[round(a, 5), round(b, 5)] for a, b in _simplify(shapes[sh], tol=25.0)]
        out[mode].append({"route": short, "name": names[(mode, short)], "pts": pts})
    cache["network"] = dict(out)
    if cache.get("feed") == zsig:
        cache_path.write_text(json.dumps(cache))
    return cache["network"]
