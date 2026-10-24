# go card travel tracker — project plan

Track and visualise public transport usage from Translink go card travel history
(<https://gocard.translink.com.au/webtix/tickets-and-fares/go-card/online/history>).

The site only exports **6 months at a time**, so every download is merged into one
master file, `data/history.csv`, de-duplicating overlaps.

This file is the source of truth for where the project is up to. Update the
checkboxes as phases land.

---

## Layout

```
.env                      # USERNAME / PASSWORD for the go card site (never committed)
PLAN.md                   # this file
data/
  history.csv             # MASTER: every trip ever downloaded, same columns as an export (gitignored)
  raw/<YYMMDD>-<YYMMDD>/*.csv  # untouched copy of each download, a backup (gitignored)
  stops.json              # YOUR go card stop name -> GTFS stop_id map (gitignored, auto-created)
  stops.example.json      # format example for stops.json (committed)
  gtfs/stops.txt          # Translink GTFS stop list, cached (gitignored)
scripts/
  fetch_gtfs.py           # downloads Translink's public GTFS feed (stop coordinates)
  fetch_history.py        # logs in to go card online, downloads and merges into history.csv
  history.py              # the master file: read, merge (de-dupe), save
  build.py                # history.csv -> dashboard/data.js
dashboard/
  index.html, app.js, style.css   # static dashboard, open index.html directly
  data.js                 # generated, personal (gitignored)
```

Personal data (`.env`, `data/history.csv`, `data/raw/`, `data/stops.json`, `dashboard/data.js`) is
gitignored so the repo can be published. Before any push, check
`git status` / `git log --stat` to confirm none of these were ever committed.

## Data notes (learned from the first export)

CSV columns: `Date,Time,From,Time,To,Fare,Credit,Balance` (note the duplicate `Time`).

- Dates look like `27/Apr/2026`. Times are **24h with a redundant suffix**: `17:20 PM`.
- `From` ending in `(Touch on Transfer)` = a transfer leg (usually `$ 0.00`), part of the previous leg's journey.
- `From` = `Auto top up(<stop>)` = a top-up row: empty To/Fare, amount in `Credit`.
- `To` = `Failed to touch Off`, arrival time `Unknown` = penalty fare.
- Stop names are go card labels (for example `Cultural Centre station - Platform 2`), which don't
  match GTFS names (`Cultural Centre station, platform 2`), hence `data/stops.json`.
- Every value has stray leading/trailing spaces, and money is formatted like `$ 0.50`.
- Older history has more row types:
  - `Top Up( Eagle Farm Depot )`: topped up with the driver. One row is both the top-up
    (Credit `30.00`, no `$`) and that trip (To/Fare filled). The "depot" is the bus's depot,
    so the boarding stop is unknown.
  - `Adjustment(<stop>)` / `Top up/Adjustment(<stop>)`: signed amounts in `Credit` (for example a reversed top-up).
  - A penalty row can have no touch-on time or stop at all (`07/Mar/2024,,,Unknown,Failed to touch Off,$ 5.00`).

## How the site works (for fetch_history.py)

- Login: `POST https://gocard.translink.com.au/webtix/welcome/welcome.do` with
  `cardOps=Display`, `cardNum=<16 digits>`, `pass=<password>`. No CSRF token or captcha.
  Success redirects to `.../online/summary`. Plain `requests` works, no browser needed.
- Export: `GET /webtix/cardinfo/history.do?csvExport=yes&startDate=DD/MM/YYYY&endDate=DD/MM/YYYY`
  returns the same CSV as the "Export data to CSV file" link.
- Ranges over ~6 months come back as an **empty body** (not an error page), so the script
  uses 180-day windows. Windows before a card's first trip return only a header row.
- The site sits behind Imperva/Incapsula. One session, a 2 s delay between requests and a
  single login attempt per run (never retry a failed login, which risks locking the account).

## Phases

### Phase 1 — dashboard from CSV  ✅ (first version)
- [x] Project scaffold, `.gitignore` for secrets and personal data
- [x] `fetch_gtfs.py`: cache GTFS `stops.txt`
- [x] `build.py`: merge and de-dupe all CSVs, group transfers into journeys, top-ups, failed touch-offs
- [x] Stop geocoding: fuzzy match go card labels to GTFS stops, store in `stops.json`, flag guesses
- [x] Dashboard: KPIs, filters, map (stops and routes), calendar, time-of-day by weekday heatmap,
      weekly trips, balance over time, top routes, trip-duration histogram, full trip table
- [ ] Ideas: compare cost against the old fare or a monthly pass, CO₂ vs. driving, "streaks"

### Phase 2 — automated retrieval  ✅
- [x] Inspect the site: login form, export URL, 6-month limit (see "How the site works")
- [x] `scripts/fetch_history.py`: logs in with `.env` credentials and saves 180-day windows to
      `data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv`. Uses plain `requests`.
- [x] `--all` walks back to the card's first trip. The default is incremental: from 7 days before
      the newest trip held, to today. `--from/--to` for explicit ranges.
- [x] `build.py` de-dupes on the touch-on (date, time, stop), newest file wins, so overlapping and
      re-downloaded windows never double count. It also parses driver top-ups, adjustments and
      penalty rows with no time.
- [x] Stop matcher: platform-aware, `(St 144)` = stop 144, aliases (RBWH). 84 new stops from the
      full history matched, 7 approximate ones left flagged in `stops.json`.
- [x] Dashboard handles multi-year ranges: a calendar row per year, monthly bars past ~6 months,
      "This year" and "Last 12 months" presets
- [x] Master file `data/history.csv`: each download is merged in. Rows are keyed on the touch-on
      (date, time, from) plus a repeat count. A changed row is updated, but a completed trip is
      never replaced by an open one. `--import PATH` merges hand-made exports with no login.
- [x] Any-length ranges: `--years N` or `--from/--to` are split into 180-day windows
      (`--years 6` tested)
- [ ] Multiple cards, if the account has more than one (one card per login on this site, so
      probably several `.env` profiles)

### Phase 3 — run it on a schedule
- [ ] One command: `make update` = fetch history, then build
- [ ] cron or systemd timer (weekly), plus a notification on login failure or site changes
- [ ] Optional: serve the dashboard locally or publish a privacy-scrubbed version

### Phase 4 — richer analysis (optional)
- [ ] Draw the actual route path (GTFS `shapes.txt`) instead of straight lines
- [ ] Infer the route number from stop pairs and time (GTFS `stop_times.txt`)
- [ ] On-time vs. timetable comparison

## How to run (current)

```bash
python3 scripts/fetch_gtfs.py           # once, or when stops look stale
python3 scripts/fetch_history.py --all  # first time: the whole history (or --years 6)
python3 scripts/fetch_history.py        # afterwards: just what's new, merged into history.csv
python3 scripts/fetch_history.py --from 2024-01-01 --to 2025-06-30   # any range, any length
python3 scripts/fetch_history.py --import ~/Downloads/Travel_history.csv   # a manual export
python3 scripts/build.py                # rebuild dashboard/data.js from history.csv
xdg-open dashboard/index.html   # or: python3 -m http.server -d dashboard
```

`build.py` takes `--history`, `--stops`, `--gtfs` and `--out` to point at other locations
(see `python3 scripts/build.py --help`). New stop names are auto-matched and saved
to `stops.json` with `"auto": true`. The dashboard flags them, so check and fix the
`stop_id` and delete the flag.
