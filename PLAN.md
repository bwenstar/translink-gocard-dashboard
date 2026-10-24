# translink-gocard-dashboard — project plan

Track and visualise public transport usage from Translink go card travel history
(<https://gocard.translink.com.au/webtix/tickets-and-fares/go-card/online/history>).

The site only exports **6 months at a time**, so every download is merged into one
master file, `data/history.csv`, de-duplicating overlaps.

This file is the source of truth for where the project is up to. Update the
checkboxes as phases land, and update README.md in the same commit as any
user-visible change (commands, flags, layout, features, limitations).

---

## Layout

```
.env                      # USERNAME / PASSWORD for the go card site (never committed)
README.md                 # user-facing docs, kept in sync with every change
PLAN.md                   # this file
requirements.txt, .env.example
data/
  history.csv             # MASTER: every trip ever downloaded, same columns as an export (gitignored)
  raw/<YYMMDD>-<YYMMDD>/*.csv  # untouched copy of each download, a backup (gitignored)
  stops.json              # YOUR go card stop name -> GTFS stop_id map (gitignored, auto-created)
  stops.example.json      # format example for stops.json (committed)
  gtfs/                   # Translink GTFS zip, stops.txt, paths_cache.json (gitignored)
scripts/
  fetch_gtfs.py           # downloads Translink's public GTFS feed (stop coordinates)
  fetch_history.py        # logs in to go card online, downloads and merges into history.csv
  history.py              # the master file: read, merge (de-dupe), save
  build.py                # history.csv -> dashboard/data.js
  routes.py               # likely route + path per leg from the GTFS timetable
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
- Stop names are go card labels (for example `Chermside interchange - Stop B`), which don't
  match GTFS names (`Chermside Shopping Centre interchange B`), hence `data/stops.json`.
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
- Ranges over ~6 months come back with **no rows** (not an error page), so the script uses
  180-day windows. Since 2026-10-24 that's a header-only CSV (it used to be an empty body),
  which looks exactly like a window with no trips. So keep windows under the limit;
  `--check-site` tests the limit separately. Windows before a card's first trip also return
  only a header row.
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
- [x] Interactive v2:
  - modes (bus/train/ferry/light rail, colour per mode, shown even with 0 trips)
  - rail & ferry network layer, map legend, fit-to-trips
  - filters for mode, route, time of day, trip type and search; removable filter chips; filters
    stored in the URL hash
  - clickable heatmap, bars, route/stop/pair lists and map lines
  - new KPIs: distance, longest streak, busiest day, routes used
  - new charts: by mode, top route numbers, most used stops, spending per month
  - CSV export of the filtered trips
- [x] "If you'd driven" KPI: the same distance at the ATO's 88 c/km (2025–26; excludes parking) and
      the NTC fleet average of 190.8 g CO₂/km. PT's own emissions aren't subtracted: there's no fair
      per-passenger figure for Brisbane buses, trains or ferries.
- [ ] Ideas: cost against pre-50c zone fares (needs historical zone data), replay animation

### Phase 2 — automated retrieval  ✅
- [x] Inspect the site: login form, export URL, 6-month limit (see "How the site works")
- [x] `scripts/fetch_history.py`: logs in with `.env` credentials and saves 180-day windows to
      `data/raw/<YYMMDD>-<YYMMDD>/Travel_history.csv`. Uses plain `requests`.
- [x] `--all` walks back to the card's first trip. The default is incremental: from 7 days before
      the newest trip held, to today. `--from/--to` for explicit ranges.
- [x] `build.py` de-dupes on the touch-on (date, time, stop), newest file wins, so overlapping and
      re-downloaded windows never double count. It also parses driver top-ups, adjustments and
      penalty rows with no time.
- [x] `scripts/review_stops.py` (`make review-stops`) checks the stop guesses against the
      timetable stops the matched services used: confirmed / suggest / check, and `--apply` saves
      the confirmations plus corrections backed by 2+ rides. It resolved all 7 leftover guesses:
      6 confirmed, 1 moved to the stop that replaced a removed one.
- [x] Stop matcher: platform-aware, `(St 144)` = stop 144, aliases (RBWH). 84 new stops from the
      full history matched, 7 approximate ones left flagged in `stops.json`.
- [x] Dashboard handles multi-year ranges: a calendar row per year, monthly bars past ~6 months,
      "This year" and "Last 12 months" presets
- [x] Master file `data/history.csv`: each download is merged in. Rows are keyed on the touch-on
      (date, time, from) plus a repeat count. A changed row is updated, but a completed trip is
      never replaced by an open one. `--import PATH` merges hand-made exports with no login.
- [x] Any-length ranges: `--years N` or `--from/--to` are split into 180-day windows
      (`--years 6` tested)
- [x] Multiple cards: `USERNAME_2`/`PASSWORD_2`/`CARD_NAME_2` and so on (`scripts/cards.py`),
      `fetch_history.py --card N`, a file per card (card 1's paths unchanged). update.py logs in to
      each card with its own login pause, and build.py tags legs and top-ups and groups journeys per
      card. Dashboard: card filter, Card column, one balance line per card.
      `.gitignore` now ignores all of `data/` except the example, because the per-card files were
      initially NOT ignored.

### Credentials & publishing hygiene
- [x] Credentials only go to gocard.translink.com.au over HTTPS (host checked after login), never
      printed or logged; `--ask` / no PASSWORD = prompt with getpass; warn if `.env` isn't chmod 600;
      refuse the `.env.example` placeholder card
- [x] Leaflet loaded with SRI hashes; README "Security & privacy" lists every network request
- [ ] Never put personal examples (your stops, card digits, dates) in committed docs. Grep before pushing.

### Resilience to site changes
- [x] `docs/site-contract.json`: login form, export, limits, CSV columns, row types (as of 2026-10-24)
- [x] Every run checks the login form before sending credentials and the CSV header of every download
- [x] `--check-site`: full check plus private raw snapshot in `data/site-snapshots/`
- [x] `tests/` (87 tests, no network; `tests/fakes.py` = synthetic GTFS feed + fake go card site):
      fetch_history (credentials, windows, login/export safety, full runs), routes, build (incl. a
      full build), history, make_example; GitHub Actions on push (Python 3.10 + 3.14; 3.9 is no longer offered on the Ubuntu 26.04 runners). Each safety rule was verified by
      breaking it on purpose (the test must fail).
- [x] Bug found by the tests: a service that doesn't run on the trip's weekday could still be
      picked using its times from other days. Fixed in routes.py.
- [x] 6-month limit re-checked (2026-10-24, second check): unchanged. A too-long range now
      returns a header-only CSV instead of an empty body; `--check-site` reports that as unchanged.

### Example data & screenshots
- [x] `scripts/make_example.py`: a fictional commuter's year from real GTFS trips (bus, train, ferry,
      light rail, transfers, top-ups, failed touch-offs) -> `examples/`; `build.py --demo` uses it
- [x] README screenshots in `docs/screenshots/` from the example data. To refresh them, build
      `--demo --out` into a copy of `dashboard/`, capture at 1280 px wide with headless Chrome, and
      crop each section.

- [x] Live demo on GitHub Pages (`.github/workflows/pages.yml`): `build.py --demo` in CI, with a
      "fictional example data" banner (`demo: true` in data.js) linking back to the repo

### Phase 3 — run it on a schedule  ✅
- [x] One command: `make update` (`scripts/update.py`). It refreshes the GTFS feed when older than
      30 days, fetches incrementally (first run: `--import data/raw` or `--all`), then builds.
      It writes `data/update.log` and `data/last-update.json`, and only one run happens at a time.
- [x] Weekly timer: `make schedule` (`scripts/schedule.py`) installs a systemd user timer
      (Mon 07:00, Persistent, 30 min jitter), or prints a cron line without systemd.
      Not installed on the dev machine yet; that's the user's call.
- [x] Notifications on failure (notify-send / osascript, plus `--notify-cmd` /
      `GOCARD_NOTIFY_CMD` for phone alerts). Failure kinds: login / site-changed / network / error.
      After a failed login, automatic runs pause until `.env` changes (or `--force`).
- [x] Always-on deployment (a Raspberry Pi on the home network): weekly cron update and
      `scripts/serve.py` as a systemd service, password-protected (HTTP Basic, refuses
      non-localhost without a password), LAN/VPN only. Pi 3: build ~4 s; cold route cache
      ~100 s / 166 MB. Docker not used (32-bit Pi 3, 1 GB).
- [x] `make serve`, a local web server for the dashboard. The privacy-scrubbed public version is
      covered by the fictional-data demo on GitHub Pages.

### Phase 4 — richer analysis (optional)
- [x] Draw the actual route path (GTFS `shapes.txt`) instead of straight lines. Dashed straight
      lines where no service matches; a "Straight lines" toggle on the map.
- [x] Infer the route number from stop pairs and time (GTFS `stop_times.txt`): trips calling at
      stops within 200 m of the tap-on, then the tap-off; nearest departure on the same weekday.
      Uses the current timetable, so older trips are a best guess; pairs with no current service
      stay straight. Cached in `data/gtfs/paths_cache.json`.
- [x] Ride time vs timetable (tap-on to tap-off minus the matched service's scheduled time), by
      hour and by route, plus a KPI and a table column. What the first attempt showed:
      - comparing old rides with today's timetable is misleading: re-routed services matched a
        stop on a different path (a "timetabled" time nearly double the real ride), and rides
        from before the network redesign looked 15+ min "faster" against today's peak padding;
      - so only rides inside the feed's own date range are compared, and only within 100 m
        (summed) of your stops. Results persist in `data/timetable-checks.json` because each
        feed covers only ~2 months. Real data starts empty and fills as the Pi updates.
      - departure punctuality isn't measured: nearest-departure matching can't tell a late
        bus from the next one, so it would be biased towards "on time".

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
