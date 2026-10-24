# Shortcuts. Each target is one script; on Windows run the python3 line directly.
PY ?= python3

.PHONY: update build demo test check-site gtfs serve schedule schedule-status unschedule

update:           ## fetch new trips, refresh the timetable when stale, rebuild
	$(PY) scripts/update.py
build:            ## rebuild dashboard/data.js from data/history.csv
	$(PY) scripts/build.py
demo:             ## build the dashboard from the fictional example data
	$(PY) scripts/build.py --demo
test:
	$(PY) -m unittest discover tests
check-site:       ## check go card online still works as docs/site-contract.json says
	$(PY) scripts/fetch_history.py --check-site
gtfs:             ## re-download Translink's timetable
	$(PY) scripts/fetch_gtfs.py
serve:            ## view the dashboard at http://localhost:8000 (see scripts/serve.py for the network)
	$(PY) scripts/serve.py
schedule:         ## run `make update` weekly (systemd user timer, or prints a cron line)
	$(PY) scripts/schedule.py install
schedule-status:
	$(PY) scripts/schedule.py status
unschedule:
	$(PY) scripts/schedule.py remove
