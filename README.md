# spot-weather-fetcher

Hourly multi-model weather forecasts for specific backcountry trail points —
published as a single public JSON file for downstream use.

Think of it as a private, automated SpotWx for a fixed set of coordinates:
the same multi-model transparency, but for exact locations you choose (a
ridge crux, a pass), with no map clicking and no UI.

**Public output:**
https://raw.githubusercontent.com/rtwalker85/spot-weather-fetcher/main/latest.json

## What it produces

For each tracked location, hourly forecasts out to 5 days from 7 models,
kept separate rather than blended so model disagreement stays visible:

| Model | Coverage |
|---|---|
| HRDPS Continental (Canada, 2.5 km) | ~2 days, highest resolution |
| RDPS (Canada regional, 10 km) | ~3.5 days |
| GDPS (Canada global, 15 km) | 5 days |
| HRRR (US, 3 km) | ~2 days, high resolution |
| GFS (US global) | 5 days |
| ECMWF IFS (0.25°) | 5 days |
| ECMWF AIFS (AI model, 0.25°) | 5 days |

Variables: temperature, cloud cover (total / low / mid / high **and at trail
elevation**), wind speed / gusts / direction, precipitation, snowfall, CAPE,
plus visibility and freezing level where the model publishes them. Air
quality (PM2.5, PM10, US AQI, aerosol optical depth for wildfire smoke)
comes from CAMS and is listed once per location.

`latest.json` carries a `note` field explaining units, caveats, and how to
read it — it's self-contained, so a consumer needs nothing from this repo.

### Cloud at trail elevation

The most useful field is `cloud_cover_at_elevation_pct`.

A cloud-ceiling height is ambiguous in the mountains: `cloud_cover_low` of
100% at a 2,700 m ridge might mean you're socked in — or that you're in
sunshine above a valley inversion. So instead of a ceiling, the pipeline
fetches cloud cover at several pressure levels plus the geopotential height
of each, then interpolates cloud cover at the location's **real elevation**.
That answers whether the crux itself is in cloud.

Comparing `cloud_cover_at_elevation_pct` against `cloud_cover_low_pct`
distinguishes "socked in" from "above the inversion".

## Adding or changing locations

Edit [`locations.json`](locations.json) — no code changes needed:

```json
{
  "id": "some_pass",
  "route": "Route Name",
  "label": "north pass, from GPX track",
  "lat": 50.12345,
  "lon": -115.12345,
  "elevation_m": 2400,
  "active": true
}
```

`elevation_m` matters: it's what cloud-at-elevation interpolates to. Set
`active: false` to keep an entry without fetching it.

Existing points were derived from GPX tracks by finding each route's real
high point / crux (elevation-profile peak detection), not the trailhead —
the weather that matters is where the exposure is.

## How it runs

[`.github/workflows/fetch.yml`](.github/workflows/fetch.yml) runs
[`fetch_weather.py`](fetch_weather.py) hourly on GitHub Actions and commits
`latest.json` back to the repo. Three API calls, roughly 4 seconds. Runs on
GitHub's servers, so nothing depends on a local machine being on.

Run it locally with:

```bash
pip install -r requirements.txt && python fetch_weather.py
```

## Why a point API instead of raw GRIB2

An earlier version pulled GRIB2 files directly from Environment Canada and
NOAA via [Herbie](https://github.com/blaylockbk/Herbie). It worked, but a
GRIB2 message is one variable, one level, one timestep for an **entire
continent** (HRDPS is 3.3 M grid points), and it can't be subset spatially.
Extracting a handful of points meant downloading continents and discarding
almost all of it:

| | Raw GRIB2 | Open-Meteo point API |
|---|---|---|
| HTTP requests per run | ~3,389 | 3 |
| Downloaded per run | ~3.9 GB | ~600 KB |
| Wall time | 30–60 min | ~4 sec |

Services like SpotWx pay that download cost once per model run and amortize
it across many users. Open-Meteo does the same and exposes it as a free
point API — while still returning **per-model** values, which is the
property worth keeping.

It's also more accurate here. Open-Meteo downscales to a 90 m elevation
model and applies lapse-rate correction, so terrain is resolved far better
in steep country:

| Northover Ridge (real 2,788 m) | Resolved elevation | Error |
|---|---|---|
| Raw HRDPS grid | 2,342 m | −446 m |
| Open-Meteo | 2,755 m | −33 m |

A 446 m error biases temperature roughly 3 °C too warm at a crux point.

The GRIB implementation remains in this repo's git history if it's ever
needed (e.g. for a true cloud-ceiling value, which the point API doesn't
expose).

## Limitations

- **No true cloud ceiling.** Cloud-at-elevation replaces it, and is arguably
  better for this purpose, but there's no metres-above-ground ceiling number.
- **RAP and NAM** aren't offered by the API. Both are short-range US models
  largely redundant with HRRR here.
- **Values are downscaled, not raw model output.** Better for point
  forecasting; not what you'd want for grid-level meteorological analysis.
- **Third-party dependency.** Open-Meteo is free for non-commercial use and
  open-source/self-hostable, but it is a single upstream.
- **Model run times aren't exposed** by the API, so `latest.json` reports
  when it was generated rather than when each model was issued.
