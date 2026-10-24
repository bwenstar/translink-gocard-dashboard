"""Shared test fixtures: a tiny synthetic GTFS feed and a fake go card online site.

Nothing here touches the network. The feed is a made-up town ("Testville") small enough
to reason about by hand; the fake site behaves like go card online as recorded in
docs/site-contract.json (login form, 180-day export limit, CSV format).
"""
import csv
import datetime as dt
import io
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# ---------- synthetic GTFS ----------
#  A ---- B       bus 10 (weekdays 08:00, 08:30), bus 20 (weekends 08:10, a detour north)
#  X ==== Y       train XVYT (weekdays 07:00, weekends 10:00) between two stations; next to
#                 each station a bus stop (XB/YB) served by bus 30 (07:04) and a rail
#                 replacement bus R99 (07:05) - both must lose to the train
#  F ~~~~ G       ferry F9;  L ---- M  light rail L9 (weekends)
STOPS = [
    # stop_id, name, lat, lon, location_type, parent
    ("A", "Alpha Rd at Testville, stop 1", -27.4000, 153.0000, "0", ""),
    ("B", "Beta St at Testville, stop 2", -27.4000, 153.0200, "0", ""),
    ("place_xsta", "Xville station", -27.4500, 153.0000, "1", ""),
    ("X1", "Xville station, platform 1", -27.4500, 153.0000, "0", "place_xsta"),
    ("place_ysta", "Yton station", -27.4500, 153.0500, "1", ""),
    ("Y1", "Yton station, platform 1", -27.4500, 153.0500, "0", "place_ysta"),
    ("XB", "Xville station bus stop", -27.4503, 153.0003, "0", ""),
    ("YB", "Yton station bus stop", -27.4503, 153.0503, "0", ""),
    ("F", "Fern ferry terminal", -27.4700, 153.0000, "0", ""),
    ("G", "Gum ferry terminal", -27.4700, 153.0200, "0", ""),
    ("L", "Lake light rail station", -27.4900, 153.0000, "0", ""),
    ("M", "Moor light rail station", -27.4900, 153.0100, "0", ""),
    ("FAR", "Alpha Rd at Farville, stop 1", -26.2000, 152.6000, "0", ""),  # same street name, 150 km away
]
ROUTES = [
    ("R10", "10", "Testville - City", "3"),
    ("R20", "20", "Testville weekend loop", "3"),
    ("RT1", "XVYT", "Xville - Yton", "2"),
    ("RRB", "R99", "Rail Replacement Bus", "3"),
    ("R30", "30", "Station shuttle", "3"),
    ("RF", "F9", "Fern - Gum", "4"),
    ("RL", "L9", "Lake line", "0"),
]
TRIPS = [  # route, service, trip, shape
    ("R10", "WK", "t10a", "SH10"), ("R10", "WK", "t10b", "SH10"), ("R20", "WE", "t20", "SH20"),
    ("RT1", "WK", "tT", "SHT"), ("RT1", "WE", "tTw", "SHT"), ("RRB", "WK", "tRR", "SHB"),
    ("R30", "WK", "t30", "SHB"), ("RF", "WE", "tF", "SHF"), ("RL", "WE", "tL", "SHL"),
]
STOP_TIMES = [  # trip, stop, time, seq
    ("t10a", "A", "08:00:00", 1), ("t10a", "B", "08:15:00", 2),
    ("t10b", "A", "08:30:00", 1), ("t10b", "B", "08:45:00", 2),
    ("t20", "A", "08:10:00", 1), ("t20", "B", "08:40:00", 2),
    ("tT", "X1", "07:00:00", 1), ("tT", "Y1", "07:12:00", 2),
    ("tTw", "X1", "10:00:00", 1), ("tTw", "Y1", "10:12:00", 2),
    ("tRR", "XB", "07:05:00", 1), ("tRR", "YB", "07:30:00", 2),
    ("t30", "XB", "07:04:00", 1), ("t30", "YB", "07:25:00", 2),
    ("tF", "F", "09:00:00", 1), ("tF", "G", "09:20:00", 2),
    ("tL", "L", "09:00:00", 1), ("tL", "M", "09:10:00", 2),
]
SHAPES = {
    "SH10": [(-27.40, 152.99), (-27.40, 153.00), (-27.40, 153.005), (-27.40, 153.01),
             (-27.40, 153.015), (-27.40, 153.02), (-27.40, 153.03)],
    "SH20": [(-27.40, 153.00), (-27.39, 153.00), (-27.39, 153.02), (-27.40, 153.02)],
    "SHT": [(-27.45, 153.00), (-27.455, 153.025), (-27.45, 153.05)],
    "SHB": [(-27.4503, 153.0003), (-27.46, 153.025), (-27.4503, 153.0503)],
    "SHF": [(-27.47, 153.00), (-27.475, 153.01), (-27.47, 153.02)],
    "SHL": [(-27.49, 153.00), (-27.49, 153.01)],
}


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def stops_rows():
    return [{"stop_id": s, "stop_name": n, "stop_lat": str(la), "stop_lon": str(lo),
             "location_type": t, "parent_station": p} for s, n, la, lo, t, p in STOPS]


def gtfs_dict():
    """What build.load_gtfs returns: stop_id -> stops.txt row."""
    return {r["stop_id"]: r for r in stops_rows()}


def make_gtfs(folder):
    """Write SEQ_GTFS.zip and stops.txt into folder. Returns (zip_path, stops_path)."""
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stops_txt = _csv(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"],
                     [list(r.values()) for r in stops_rows()])
    files = {
        "stops.txt": stops_txt,
        "routes.txt": _csv(["route_id", "route_short_name", "route_long_name", "route_type"], ROUTES),
        "calendar.txt": _csv(["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                              "sunday", "start_date", "end_date"],
                             [["WK", 1, 1, 1, 1, 1, 0, 0, "20250101", "20271231"],
                              ["WE", 0, 0, 0, 0, 0, 1, 1, "20250101", "20271231"]]),
        "trips.txt": _csv(["route_id", "service_id", "trip_id", "shape_id"], TRIPS),
        "stop_times.txt": _csv(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                               [(t, tm, tm, s, q) for t, s, tm, q in STOP_TIMES]),
        "shapes.txt": _csv(["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
                           [(sid, la, lo, i + 1) for sid, pts in SHAPES.items() for i, (la, lo) in enumerate(pts)]),
    }
    zip_path = folder / "SEQ_GTFS.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    (folder / "stops.txt").write_text(stops_txt)
    return zip_path, folder / "stops.txt"


def leg(frm, to, on, **kw):
    """A leg as build.parse_legs makes it (the fields routes.infer reads)."""
    return {"from": frm, "to": to, "on": on, "timeUnknown": False, **kw}


# ---------- fake go card online ----------
HEADER = " Date,Time,From,Time,To,Fare,Credit,Balance"
LOGIN_PAGE = """<html><body>
<form id="search" action="/search" method="get"><input type="text" name="query"></form>
<form id="login-form" action="/webtix/welcome/welcome.do" method="post">
  <input type="hidden" name="cardOps" value="Display">
  <input type="text" name="cardNum"><input type="password" name="pass">
  <input type="submit" value="Login">
</form></body></html>"""
CARD, PASSWORD = "1111222233334444", "correct horse"


def trip_row(day, n=0):
    return (f" {day:%d/%b/%Y},08:{n % 60:02d} AM,Alpha Road - 1,08:{(n + 15) % 60:02d} AM,Beta Street - 2,"
            f"$ 0.50,,$ 10.00 ")


class FakeResponse:
    def __init__(self, text="", url="", status=200):
        self.text, self.url, self.status_code = text, url, status
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeSite:
    """Holds the 'server' state; .session() is patched in for requests.Session."""
    base = "https://gocard.translink.com.au/webtix"

    def __init__(self, first_trip=None, last_trip=None, every_days=3, login_page=LOGIN_PAGE,
                 redirect_host=None, max_days=180, export_override=None):
        today = dt.date.today()
        self.first = first_trip or today - dt.timedelta(days=400)
        self.last = last_trip or today
        self.days = [self.first + dt.timedelta(days=i) for i in range(0, (self.last - self.first).days + 1, every_days)]
        self.login_page, self.redirect_host, self.max_days = login_page, redirect_host, max_days
        self.export_override = export_override  # callable(start, end) -> text, to simulate site changes
        self.posts, self.exports, self.logouts, self.logged_in = [], [], 0, False

    def session(self):
        site = self

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, params=None, timeout=None):
                return site.get(url, params or {})

            def post(self, url, data=None, headers=None, timeout=None):
                return site.post(url, data or {})

        return Session()

    def get(self, url, params):
        if url.endswith("/welcome/welcome.do"):
            if params.get("logout"):
                self.logouts += 1
                self.logged_in = False
            return FakeResponse(self.login_page, url)
        if url.endswith("/cardinfo/history.do"):
            if not self.logged_in:
                return FakeResponse(self.login_page, url)
            if params.get("csvExport") != "yes":
                return FakeResponse('<html><a href="history.do?csvExport=yes">Export</a></html>', url)
            start = dt.datetime.strptime(params["startDate"], "%d/%m/%Y").date()
            end = dt.datetime.strptime(params["endDate"], "%d/%m/%Y").date()
            self.exports.append((start, end))
            if self.export_override:
                return FakeResponse(self.export_override(start, end), url)
            if (end - start).days + 1 > self.max_days:
                return FakeResponse("", url)  # too long: empty body, like the real site
            rows = [trip_row(d, i) for i, d in enumerate(self.days) if start <= d <= end]
            return FakeResponse("\n".join([HEADER] + rows) + "\n", url)
        return FakeResponse("not found", url, 404)

    def post(self, url, data):
        self.posts.append(dict(data))
        if self.redirect_host:
            return FakeResponse("<html>elsewhere</html>", f"https://{self.redirect_host}/login")
        if data.get("cardNum") == CARD and data.get("pass") == PASSWORD and data.get("cardOps") == "Display":
            self.logged_in = True
            return FakeResponse("<html>summary</html>", f"{self.base}/online/summary")
        return FakeResponse(self.login_page, f"{self.base}/welcome/welcome.do")
