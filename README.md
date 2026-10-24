# go card travel tracker

Download your [Translink](https://translink.com.au) go card travel history and explore it
on a local dashboard: trips on a map, a calendar, when you travel, top routes, time on board,
card balance and a searchable trip table.

go card online only exports **6 months at a time**. This project gets around that by splitting
any date range into 6-month windows, downloading each one, and merging them into a single master
file (`data/history.csv`) with overlaps removed. You can download as far back as your card's
history goes and then just fetch what's new.

Everything runs locally. Your card number, password and travel history stay on your machine and
are gitignored.

## Features

- **Automated download** from go card online with your card number and password. It uses plain
  HTTP requests, so no browser is needed.
- **Any date range**: `--years 6`, `--from/--to`, or `--all` to walk back to your first trip.
- **One master file**: every download is merged into `data/history.csv` with no double counting.
  A trip that was still open in an earlier download is updated once it completes.
- **Import manual exports**: merge CSVs you downloaded from the website yourself.
- **Stop geocoding**: go card stop names are matched to Translink's public GTFS stop list. Matching
  is fuzzy but platform-aware, and anything uncertain is flagged for you to check.
- **Route paths and route numbers**: each trip is matched against Translink's timetable to find
  the service you most likely caught. The map draws its real path along the roads, busway, rail
  line or river, and the trip table shows the route number.
- **Every mode**: bus, train, ferry (CityCat and cross-river) and light rail (G:link) are detected,
  coloured and filterable. A mode you haven't used yet still shows up, with zero trips.
- **Static dashboard**: open `dashboard/index.html` straight from disk. There's no server and no
  build tooling, and it supports light and dark mode.
  - KPIs: journeys, fares, distance travelled, time on board, days travelled, longest daily
    streak, busiest day, routes used, failed touch-offs, top-ups
  - **map**:
    - trips drawn along their real route path, coloured by mode and sized by use
    - an optional layer showing the whole rail and ferry network
    - a mode legend, a straight-line toggle and a "fit to trips" button
    - click a line or stop to filter
  - **charts**:
    - trips-per-day calendar, weekday × hour heatmap, weekly/monthly trips
    - legs by mode, top route numbers, top stop pairs, most used stops
    - trip durations, spending per month, card balance with top-ups
  - **filters**:
    - date range and presets, weekdays, mode, route number, time of day
    - trip type (with a transfer, failed touch-off, no route match, unverified stop)
    - stop, and a free-text search
  - **every chart is clickable**: a heatmap cell filters to that weekday and hour, a bar zooms
    to that week or month, and a route, stop or stop pair filters to it. Active filters show as
    removable chips.
  - filters are saved in the URL, so a filtered view can be bookmarked
  - a sortable trip table with mode, route, distance and fare. Click a row to show that journey
    on the map. **Download CSV** exports exactly the trips you've filtered to.

## Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/) (`pip install -r requirements.txt`)
- A go card registered for go card online, with a password

## Quick start

```bash
git clone <this repo> && cd <repo>
pip install -r requirements.txt

cp .env.example .env && chmod 600 .env   # then fill in your card number (and password)
python3 scripts/fetch_gtfs.py      # once: Translink stop coordinates
python3 scripts/fetch_history.py --all
python3 scripts/build.py
xdg-open dashboard/index.html      # macOS: open, Windows: start
```

The first build takes about a minute: it matches every stop you've used to Translink's stop list
and looks up route paths. After that, builds take under a second. Check the stop guesses it prints
(see [Checking stop matches](#checking-stop-matches)).

### `.env`

```ini
# your 16-digit go card number
USERNAME=0123456789012345
PASSWORD=your-go-card-online-password
```

`GOCARD_CARD` / `GOCARD_PASSWORD` also work, in `.env` or as environment variables. `USERNAME` is
only read from `.env`, never from the environment, where it's usually your OS login name.

**Rather not store your password?** Leave `PASSWORD` out of `.env`, or run with `--ask`.
`fetch_history.py` will prompt for it each run, and it's held in memory only for that run.

## Usage

### Download history: `scripts/fetch_history.py`

| Command | What it downloads |
|---|---|
| `fetch_history.py` | **Incremental** (default): from a week before the newest trip in the master file to today |
| `fetch_history.py --all` | Walks back from today until two 6-month windows in a row are empty |
| `fetch_history.py --years 6` | The last N years |
| `fetch_history.py --from 2024-01-01 --to 2025-06-30` | An explicit range of any length (`YYYY-MM-DD` or `DD/MM/YYYY`; `--to` defaults to today) |
| `fetch_history.py --import PATH [PATH ...]` | No download: merges CSV files or folders you exported by hand |

Options:

| Option | Default | |
|---|---|---|
| `--history` | `data/history.csv` | master file to merge into |
| `--raw` | `data/raw` | where a backup copy of each download is saved, as `<YYMMDD>-<YYMMDD>/Travel_history.csv` |
| `--no-raw` | | don't keep backup copies |
| `--env` | `.env` | credentials file |
| `--ask` | | prompt for the password instead of reading it from `.env` |

Each window is merged and saved as soon as it's downloaded, so an interrupted run keeps what it got.

### Build the dashboard: `scripts/build.py`

Reads `data/history.csv` and writes `dashboard/data.js`. Run it after every download.

| Option | Default |
|---|---|
| `--history` | `data/history.csv` |
| `--stops` | `data/stops.json` (created automatically) |
| `--gtfs` | `data/gtfs/stops.txt` |
| `--gtfs-zip` | `data/gtfs/SEQ_GTFS.zip`. Full feed used for route paths; without it, lines are straight |
| `--out` | `dashboard/data.js` |

### Stop coordinates: `scripts/fetch_gtfs.py`

Downloads Translink's public [SEQ GTFS feed](https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip)
(about 35 MB) into `data/gtfs/`. It keeps the whole zip for route paths and extracts `stops.txt`
and `routes.txt`. Run it once, and again every so often to pick up timetable changes or new stops.

### Checking stop matches

go card stop labels (`Cultural Centre station - Platform 2`) don't match GTFS names, so
`build.py` guesses a GTFS stop for each new name and saves it to `data/stops.json`:

```json
"Cultural Centre station - Platform 2": { "stop_id": "11168" },
"Some Stop - Arrival": { "stop_id": "203", "auto": true, "score": 0.52 }
```

Entries with `"auto": true` are guesses. The build prints them, and the dashboard flags them on the
map and in the table. To check one, look the stop up in `data/gtfs/stops.txt` or on a map. Then
correct the `stop_id` if needed and delete `auto` and `score`. Your corrections are kept on later
builds. See `data/stops.example.json` for the format.

## Typical update

```bash
python3 scripts/fetch_history.py && python3 scripts/build.py
```

Then refresh the dashboard.

## Project layout

```
.env                        your credentials (gitignored)
.env.example                template for .env
requirements.txt
README.md                   this file
PLAN.md                     roadmap and implementation notes
scripts/
  fetch_history.py          log in, download in 6-month windows, merge into history.csv
  history.py                master file: parse, merge/de-dupe, save
  build.py                  history.csv -> dashboard/data.js, stop matching
  routes.py                 likely route per trip and its path, from the GTFS timetable
  fetch_gtfs.py             Translink GTFS stop list
data/
  history.csv               master travel history (gitignored)
  raw/                      backup copy of every download (gitignored)
  stops.json                your stop name -> GTFS stop_id map (gitignored)
  stops.example.json        format example
  gtfs/                     GTFS feed and route-path cache (gitignored)
dashboard/
  index.html, app.js, style.css
  data.js                   generated from your history (gitignored)
```

## How it works

**Downloading.** Login is a form `POST` to `/webtix/welcome/welcome.do` with your card number and
password. The CSV export is
`/webtix/cardinfo/history.do?csvExport=yes&startDate=DD/MM/YYYY&endDate=DD/MM/YYYY`. Ranges longer
than about 6 months come back empty, so requests use 180-day windows. The script is careful with
your account: one session per run, a 2-second pause between requests, and it **never retries a
failed login**, so a wrong password can't lock you out.

**Merging.** Each row is identified by its touch-on: date, time and stop, plus a count for genuine
repeats in the same minute. A row seen again is ignored if it's unchanged and updated if it changed.
A completed trip is never replaced by an open one.

**Parsing.** The export has quirks that `build.py` handles:
- a duplicate `Time` header
- 24-hour times with a redundant suffix (`17:20 PM`)
- money like `$ 0.50`
- transfer legs (`(Touch on Transfer)`)
- top-up rows: `Auto top up(...)` and driver top-ups (`Top Up(... Depot)`, where one row is both
  the top-up and a trip)
- adjustments
- failed touch-offs, including penalty rows with no time or stop

Transfer legs are grouped into journeys.

**Route paths.** For each tap-on/tap-off stop pair, `routes.py` searches the GTFS `stop_times.txt`
for trips that call at the tap-on stop and later at the tap-off stop. Stops within 200 m also count,
which covers station platforms. Each trip takes the service whose departure is nearest the tap-on
time on the same weekday. That trip's shape (`shapes.txt`) is cut between the two stops and
simplified. The first build takes about 10 seconds. Results are cached in
`data/gtfs/paths_cache.json`, so later builds take under a second until the feed or your stops change.

**Modes.** Each leg takes the mode of its matched GTFS route (bus 3, rail 2, ferry 4, light rail 0).
Unmatched legs fall back to the stops: ferry terminals and train stations are recognised from
their GTFS ids and names. Rail replacement buses in the timetable are ignored. A bus match between
two train stations is only accepted if no train fits. The network layer is one shape per rail,
ferry and light rail route, simplified to about 25 m.

**Dashboard.** Plain HTML, CSS and JS with hand-drawn SVG charts and [Leaflet](https://leafletjs.com)
for the map. Modes use three colour-blind-checked colours (bus blue, rail orange, ferry aqua).
Light rail shares the rail colour with a dashed line, because a fourth colour can't be told apart
reliably. Map tiles are Esri World Gray Canvas, which needs no API key and works from `file://`.

## Security & privacy

This is a local tool. There is no server, account or analytics, and nothing is uploaded anywhere.
Don't just take that on trust: the code is short enough to check.

### Your go card credentials

- They are read in one place, `credentials()` in `scripts/fetch_history.py`, and sent in one
  place, `login()`: a single HTTPS `POST` to `https://gocard.translink.com.au/webtix/welcome/welcome.do`,
  the same form the website uses.
- After logging in, the script checks it's still on `gocard.translink.com.au` and stops otherwise.
- They are never printed or logged (only the card's last 4 digits are shown), never written to
  any other file, and never sent to any other site.
- One login attempt per run. A failed login stops immediately rather than retrying, so a wrong
  password can't lock your card. The script logs out when it finishes.
- `.env` is gitignored. The script warns if it's readable by other users (`chmod 600 .env`).
  It also refuses to run with the placeholder card number from `.env.example`.
- To avoid storing the password at all, use `--ask` (see above).
- Use a go card password you don't use anywhere else. If you think it's been exposed, change it
  on the go card website, which makes the old one useless.

To audit it yourself, list every network call in the project:

```bash
grep -rnE "https?://|requests\.|urlopen" scripts dashboard
```

### Every network request the project makes

| From | To | What's sent | Why |
|---|---|---|---|
| `fetch_history.py` | `gocard.translink.com.au` | card number + password (login), date ranges | download your history |
| `fetch_gtfs.py` | `gtfsrt.api.translink.com.au` | nothing personal (plain download) | public timetable and stop data |
| dashboard (browser) | `unpkg.com` | nothing personal | the Leaflet map library, pinned with integrity hashes |
| dashboard (browser) | `server.arcgisonline.com` (Esri) | which map tiles to draw | background map |

Your trip data (`data.js`) is read by the browser from disk and never leaves your computer.
The one thing worth knowing: map tile requests reveal to Esri which area you're looking at, like
any web map. `build.py`, `history.py` and `routes.py` make no network requests.

### Personal files

These files are personal and are gitignored. Don't commit them:

| File | Contains |
|---|---|
| `.env` | card number and password |
| `data/history.csv`, `data/raw/` | every trip you've taken |
| `data/stops.json` | the stops you use, which reveal roughly where you live and work |
| `dashboard/data.js` | all of the above, compiled |
| `data/gtfs/paths_cache.json` | the stop pairs you travel between |

Before pushing a fork, check `git status` and
`git log --all --name-only --format= | grep -E "\.csv$|\.env$|data\.js$|stops\.json$"`.
The second command should print nothing. Also check your own notes and commit messages for stop
names or card digits before publishing a fork.

## Limitations

- Works for anyone with a go card online account in South East Queensland: nothing in the code is
  specific to one person. Stops are matched from scratch for each user (check the guesses the
  build prints).
- One card per login. The site shows one card per session, so multiple cards aren't supported yet.
- History only goes back as far as go card online keeps it for that card. A replacement card starts
  a new history.
- Trips you started by topping up with the driver have no recorded boarding stop. They show as
  "Unknown stop".
- Routes come from Translink's **current** timetable, so for older trips they are a best guess.
  If no current service links two stops (for example the route was changed or removed), the trip
  is drawn as a dashed straight line.
- This uses go card online's website, not a public API. It may break if Translink changes the site.

## Roadmap

See [PLAN.md](PLAN.md). Next up:
- `make update` and a scheduled (cron/systemd) weekly fetch with failure notifications
- on-time comparison against the timetable

## Disclaimer

This is an unofficial personal project, not affiliated with or endorsed by Translink or the
Queensland Government. Use it only with your own go card account.
