# UV Dose

A desktop app that works out how much UV your skin has actually had, using
ARPANSA's minute-by-minute measurements for Australian cities. Pick a city, a day
and a stretch of time, and it shows your dose, when you'd burn and how long it
takes to make vitamin D. It's a more flexible take on ARPANSA's own
[UV dose page](https://www.arpansa.gov.au/our-services/monitoring/ultraviolet-radiation-monitoring/ultraviolet-radiation-dose).

UV observations courtesy of ARPANSA. Cloud forecasts by
[Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0).

## Getting started

1. Install [uv](https://docs.astral.sh/uv/) if you don't have it yet, then open
   a new terminal so `uv` is on your PATH.

   Windows (PowerShell):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   macOS or Linux:

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Get the code and start the app:

   ```sh
   git clone https://github.com/Beenalf/uv-dashboard.git
   cd uv-dashboard
   uv run uv-dose
   ```

The first run takes a minute while uv downloads Python 3.13 and the libraries.
After that it starts straight away. `uv run python -m uv_dose` works too.

You don't need to install Python yourself. If uv picks up a Python you already
have, it must be 3.13 or newer and include Tk.

## What it does

- **Any city, any day since 2016, any time window.** Set the times with the
  spinboxes or presets, or drag across the chart (a click moves just the start).
- **Real outings.** Add a morning walk and a lunch break, each with its own shade,
  ground and sunscreen. Water, sand and snow reflect extra UV. Sunscreen allows
  for how thinly people really apply it (half the tested amount by default). Set
  the amount to None to see the day without it.
- **Your dose in SED** (Standard Erythemal Dose, 100 J/m²), beside ARPANSA's
  open-sun figure and the clear-sky forecast, so you can see how much cloud,
  shade and sunscreen cut it.
- **Time to burn** from your start time, based on your Fitzpatrick skin type. ARPANSA's 10-question quiz
  sets it, or you can enter your own burn threshold.
- **Vitamin D timing.** How long it takes to reach your goal (1,000 to 10,000 IU,
  or any amount) with the skin you have exposed, and the most you could make that
  day. It uses the part of UV that makes vitamin D, which fades faster than the
  burning part when the sun is low (Pope et al., 2008).
- **Cloud-aware forecasts.** ARPANSA's forecast for the rest of today assumes a
  clear sky. The app adjusts it with the last hour's measured cloud and
  Open-Meteo's cloud forecast. In a backtest of cloudy days, this cut the typical
  error from 64% to 18%. The ▶ button past today gives a rough outlook for tomorrow.
- **Chart and table.** The chart shows UV over the day and your dose building up,
  with sun protection times (UV 3 and above) and the burn and vitamin D levels
  marked. The hourly table copies as CSV.

It has light and dark themes (☰ menu) and remembers your settings in
`%APPDATA%\uv-dose\settings.json` on Windows. For the maths and its limits, see
**☰ → How the calculations work**.

## Where the data comes from

**ARPANSA.** The app reads the JSON API behind ARPANSA's own charts on
`uvdata.arpansa.gov.au`: one reading a minute from 06:00 to 20:00 local time, both
measured and clear-sky forecast. Data goes back to 2016, or later for some sites.
ARPANSA doesn't document this API officially, so it could change without notice. Links
to their disclaimer can be found in the ☰ menu and footer.

**Open-Meteo.** Hourly cloud forecasts (GFS model) from the free API, with no key
needed. The app sends only the ARPANSA site's coordinates. The free tier is for
non-commercial use and asks for attribution, which is in the app footer.

## Development

Run the full test suite, including one test against the live ARPANSA API:

```sh
uv run pytest
```

Skip the tests that need the internet:

```sh
uv run pytest -m "not network"
```

The core logic (`arpansa.py`, `clouds.py`, `dose.py`, `exposure.py`, `skin.py`,
`vitamin_d.py`, `planner.py`) doesn't depend on the GUI. The tests check it against
ARPANSA's published dose figures for a real day (Melbourne, 20 Nov 2026). The window
uses its own ttk theme (`gui/theme.py`), with rounded shapes drawn by Pillow.

## Disclaimer

This isn't medical advice. The burn and vitamin D figures come from simple
published models. Individual burn thresholds vitamin D production varies.
