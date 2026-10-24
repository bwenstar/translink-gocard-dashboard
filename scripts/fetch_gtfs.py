#!/usr/bin/env python3
"""Download Translink's SEQ GTFS feed and keep just stops.txt (used to put stops on the map)."""
import io
import pathlib
import urllib.request
import zipfile

GTFS_URL = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "gtfs"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {GTFS_URL} ...")
    with urllib.request.urlopen(GTFS_URL) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in ("stops.txt", "routes.txt"):
            (OUT_DIR / name).write_bytes(zf.read(name))
            print(f"  wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()
