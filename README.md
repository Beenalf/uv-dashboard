# UV Dose

A desktop app (Tkinter + matplotlib) that works out your personal UV dose from
ARPANSA's minute-by-minute UV measurements for Australian cities. It is a more
flexible version of ARPANSA's
[UV dose page](https://www.arpansa.gov.au/our-services/monitoring/ultraviolet-radiation-monitoring/ultraviolet-radiation-dose).

UV observations courtesy of ARPANSA.

## Run it

```sh
uv run uv-dose        # or: uv run python -m uv_dose
```

Needs Python 3.13+ (with Tk, which the python.org and uv builds include).

## What it does

- **Any time window, any city, any day since 2016.** Pick a start and end time with
  the spinboxes or presets, or drag across the chart. Click the chart to move only
  the start.
- **Dose in SED** (Standard Erythemal Dose, 100 J/m²) for the window. It is rated
  against your burn threshold and compared with the clear-sky forecast, so you can
  see how much cloud cut it.
- **Two-panel chart:** the UV Index over the day (measured vs clear-sky forecast,
  with WHO category bands), and the dose building up on your skin across the
  window, with the burn and vitamin D thresholds marked.
- **Fitzpatrick skin type:** ARPANSA's 10-question phototype questionnaire sets
  your skin type and a default burn threshold, which you can override.
- **Time to burn** from your start time, following the real UV curve, with optional
  sunscreen (SPF).
- **Vitamin D timing:** how long from your start time to reach a target in IU for
  the skin you expose. It is the "sweet spot" before extra sun mostly adds damage.
- **Hourly table**, which you can copy as CSV. There is also a dark theme, and your
  settings are remembered (in `%APPDATA%\uv-dose\settings.json` on Windows).

The calculation details and their limits are under **Help → How the calculations work**.

## Data source

The app reads the JSON API on `uvdata.arpansa.gov.au` that drives ARPANSA's own
charts. It gives one reading per minute for 06:00–20:00 local time, measured and
clear-sky forecast. ARPANSA doesn't formally document this API (only the
current-value XML feed is documented), so it may change. Per-minute data starts in
2016; some sites start later. In 2016 the forecast is only every 10 minutes, so the
app interpolates it.

ARPANSA's conditions of use for this data include:

- acknowledging ARPANSA as the source (the app footer does this);
- linking to their disclaimer (Help → ARPANSA data disclaimer);
- not linking the data to commercial promotion without permission;
- **notifying ARPANSA that the data is used in an application.** That step is up to you.

## Development

```sh
uv run pytest                  # all tests, including one live-API smoke test
uv run pytest -m "not network" # offline only
```

The core maths (`arpansa.py`, `dose.py`, `skin.py`, `vitamin_d.py`, `planner.py`)
has no GUI dependency. The tests check it against ARPANSA's published dose figures
for a real day (Melbourne, 20 Nov 2026).

Not medical advice. The burn and vitamin D figures come from simple published
models, and people vary a lot.
