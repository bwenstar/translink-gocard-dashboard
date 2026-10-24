# go card travel tracker — project plan

Track and visualise public transport usage from Translink go card travel history
(<https://gocard.translink.com.au/webtix/tickets-and-fares/go-card/online/history>).

The site only exports **6 months at a time**, so the project keeps every export it
has ever downloaded and merges them, de-duplicating overlaps, into one history.

This file is the source of truth for where the project is up to. Update the
checkboxes as phases land.

---

## Layout

```
.env                      # USERNAME / PASSWORD for the go card site (never committed)
PLAN.md                   # this file
data/
  raw/<any-folder>/*.csv  # untouched go card exports, one folder per download (gitignored)
  stops.json              # YOUR go card stop name -> GTFS stop_id map (gitignored, auto-created)
  stops.example.json      # format example for stops.json (committed)
  gtfs/stops.txt          # Translink GTFS stop list, cached (gitignored)
scripts/
  fetch_gtfs.py           # downloads Translink's public GTFS feed (stop coordinates)
  build.py                # raw CSVs -> dashboard/data.js
dashboard/
  index.html, app.js, style.css   # static dashboard, open index.html directly
  data.js                 # generated, personal (gitignored)
```

Personal data (`.env`, `data/raw/`, `data/stops.json`, `dashboard/data.js`) is
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

## Phases

### Phase 1 — dashboard from CSV  ✅ (first version)
- [x] Project scaffold, `.gitignore` for secrets and personal data
- [x] `fetch_gtfs.py`: cache GTFS `stops.txt`
- [x] `build.py`: merge and de-dupe all CSVs, group transfers into journeys, top-ups, failed touch-offs
- [x] Stop geocoding: fuzzy match go card labels to GTFS stops, store in `stops.json`, flag guesses
- [x] Dashboard: KPIs, filters, map (stops and routes), calendar, time-of-day by weekday heatmap,
      weekly trips, balance over time, top routes, trip-duration histogram, full trip table
- [ ] Ideas: compare cost against the old fare or a monthly pass, CO₂ vs. driving, "streaks"

### Phase 2 — automated retrieval
- [ ] Inspect the history page: login form fields, CSRF tokens, the export request (date range params)
- [ ] `scripts/fetch_history.py`: log in with `.env` credentials, request each 6-month window
      back to the card's start, save to `data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv`
      - first try plain `requests` + session cookies; fall back to Playwright if the site needs JS
      - be polite: one session, small delays, no parallel requests
- [ ] Incremental mode: only fetch from the latest date already held (overlap a few days, de-dupe handles it)
- [ ] Multiple cards, if the account has more than one

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
python3 scripts/fetch_gtfs.py   # once, or when stops look stale
python3 scripts/build.py        # after adding a new export to data/raw/
xdg-open dashboard/index.html   # or: python3 -m http.server -d dashboard
```

`build.py` takes `--raw`, `--stops`, `--gtfs` and `--out` to point at other locations
(see `python3 scripts/build.py --help`). New stop names are auto-matched and saved
to `stops.json` with `"auto": true`. The dashboard flags them, so check and fix the
`stop_id` and delete the flag.
