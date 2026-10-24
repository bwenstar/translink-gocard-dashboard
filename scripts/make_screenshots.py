#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/ from the fictional example data.

  python3 scripts/make_screenshots.py

Needs Chrome or Chromium (headless) and Pillow, plus data/gtfs/ from fetch_gtfs.py. It builds
the demo (build.py --demo) into a temporary copy of dashboard/, so your own data.js is never
touched or shown. Then it captures the page in light and dark mode at 1280 px and crops each
section along its edges. Screenshot-only tweaks to that copy: map animations off, the map
opened on central Brisbane, and the demo banner hidden.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
WIDTH, HEIGHT = 1280, 5200

# file name -> (first section heading, last section heading); None = from the top of the page
SHOTS = {
    "overview.png": (None, "Where you travel"),
    "charts.png": ("When you tap on", "Top route numbers"),
    "more-charts.png": ("Top stop pairs", "Spending per month"),
    "timetable.png": ("Ride time vs timetable", "Ride time vs timetable"),
    "trips.png": ("Card balance", "All trips"),
}
DARK = ("dark-rail.png", "#modes=train,tram")  # dark mode, filtered to train + light rail

MEASURE = """<script>addEventListener("load", () => setTimeout(() => {
  const o = {};
  document.querySelectorAll(".card").forEach((c) => {
    const h = c.querySelector("h2"), r = c.getBoundingClientRect();
    if (h) o[h.textContent.trim()] = [Math.round(r.top + scrollY), Math.round(r.height)];
  });
  const p = document.createElement("pre"); p.id = "rects"; p.textContent = JSON.stringify(o); document.body.append(p);
}, 2500));</script>"""


def chrome():
    for name in ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable"):
        if shutil.which(name):
            return name
    sys.exit("Needs Chrome or Chromium on PATH")


def run_chrome(browser, args):
    return subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                           "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
                           "--virtual-time-budget=15000", *args], capture_output=True, text=True, timeout=180)


def main():
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Needs Pillow: pip install pillow")
    browser = chrome()
    with tempfile.TemporaryDirectory() as tmp:
        site = pathlib.Path(tmp) / "dashboard"
        shutil.copytree(ROOT / "dashboard", site, ignore=shutil.ignore_patterns("data.js", "status.js"))
        subprocess.run([sys.executable, ROOT / "scripts" / "build.py", "--demo", "--out", site / "data.js"],
                       check=True, capture_output=True)

        app = (site / "app.js").read_text()
        for old, new in [
            ('map = L.map("map", { scrollWheelZoom: false, maxZoom: 16 });',
             'map = L.map("map", { scrollWheelZoom: false, maxZoom: 16, zoomAnimation: false, fadeAnimation: false, markerZoomAnimation: false });'),
            ("if (pts.length) map.fitBounds(pts, { padding: [16, 16] });", "if (pts.length) map.setView([-27.445, 153.03], 12);"),
        ]:
            if old not in app:
                sys.exit(f"app.js changed; update the screenshot tweak for: {old[:50]}")
            app = app.replace(old, new)
        (site / "app.js").write_text(app)
        html = (site / "index.html").read_text().replace("</head>", "<style>.demo-banner{display:none!important}</style></head>")
        (site / "index.html").write_text(html)
        (site / "measure.html").write_text(html.replace("</body>", MEASURE + "</body>"))

        dom = run_chrome(browser, ["--dump-dom", f"file://{site / 'measure.html'}"]).stdout
        m = re.search(r'<pre id="rects">(.*?)</pre>', dom, re.S)
        if not m:
            sys.exit("Couldn't measure the page")
        rects = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))

        light, dark = pathlib.Path(tmp) / "light.png", pathlib.Path(tmp) / "dark.png"
        run_chrome(browser, [f"--screenshot={light}", f"file://{site / 'index.html'}"])
        run_chrome(browser, ["--force-dark-mode", f"--screenshot={dark}", f"file://{site / 'index.html'}{DARK[1]}"])

        OUT.mkdir(parents=True, exist_ok=True)
        pad = 10
        for name, (first, last) in SHOTS.items():
            for heading in filter(None, (first, last)):
                if heading not in rects:
                    sys.exit(f"No section titled {heading!r} on the page")
            top = 0 if first is None else rects[first][0] - pad
            bottom = rects[last][0] + rects[last][1] + pad
            if name == "trips.png":  # the table is long: show its first rows
                bottom = min(bottom, rects[last][0] + 380)
            img = Image.open(light).convert("RGB").crop((0, top, WIDTH, bottom))
            img.quantize(colors=256, method=2, dither=0).save(OUT / name, optimize=True)
            print(f"{name:16} {img.size[0]}x{img.size[1]}  {(OUT / name).stat().st_size // 1024} KB")
        img = Image.open(dark).convert("RGB")
        img = img.crop((0, 0, WIDTH, rects["Where you travel"][0] + rects["Where you travel"][1] + pad))
        img.quantize(colors=256, method=2, dither=0).save(OUT / DARK[0], optimize=True)
        print(f"{DARK[0]:16} {img.size[0]}x{img.size[1]}  {(OUT / DARK[0]).stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
