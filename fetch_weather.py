#!/usr/bin/env python3
"""Fetch HRDPS point forecasts for tracked trail locations and write latest.json.

Pipeline: Herbie downloads the raw GRIB2 file from Environment Canada's public
data servers -> cfgrib/xarray decode it into a grid of numbers -> Herbie's
pick_points() finds the grid cell nearest each location in locations.json
(using real lat/lon distances, which is what makes it safe on HRDPS's rotated
grid) -> the results are written to latest.json.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from herbie import Herbie

MODEL = "hrdps"
PRODUCT = "continental"
LOCATIONS_FILE = Path(__file__).parent / "locations.json"
OUTPUT_FILE = Path(__file__).parent / "latest.json"

# The report is generated early morning; aim the forecast at local midday,
# since that's the window that matters for a trail-run report.
LOCAL_TZ = ZoneInfo("America/Edmonton")
TARGET_LOCAL_HOUR = 12

CYCLE_HOURS = 6  # HRDPS publishes a new run every 6 hours (00/06/12/18 UTC)
MAX_RUN_LOOKBACK = 4  # how many cycles to step back if the latest run isn't published yet
MAX_FXX = 48  # HRDPS's forecast length limit

# Each entry: output key, Herbie variable/level, and how to convert the raw
# decoded value into the unit we want to store.
VARIABLES = [
    {"key": "temp_c", "variable": "TMP", "level": "AGL-2m", "convert": lambda k: round(k - 273.15, 1)},
    {"key": "cloud_cover_pct", "variable": "TCDC", "level": "Sfc", "convert": lambda v: round(v, 0)},
    {"key": "wind_speed_kmh", "variable": "WIND", "level": "AGL-10m", "convert": lambda v: round(v * 3.6, 1)},
    {"key": "wind_gust_kmh", "variable": "GUST", "level": "AGL-10m", "convert": lambda v: round(v * 3.6, 1)},
    {"key": "wind_dir_deg", "variable": "WDIR", "level": "AGL-10m", "convert": lambda v: round(v, 0)},
]

NOTE = (
    "cloud_cover_pct is total cloud cover (%), not a ceiling height in feet -- "
    "HRDPS does not publish a direct cloud-ceiling variable, so total cloud "
    "cover is used as the closest available proxy. Any location with "
    "active=false in locations.json is a placeholder and is not included below."
)


def load_active_points() -> pd.DataFrame:
    data = json.loads(LOCATIONS_FILE.read_text())
    points = [p for p in data["points"] if p.get("active") and p.get("lat") is not None and p.get("lon") is not None]
    if not points:
        sys.exit("No active locations with coordinates found in locations.json -- nothing to fetch.")
    return pd.DataFrame(
        {
            "latitude": [p["lat"] for p in points],
            "longitude": [p["lon"] for p in points],
            "id": [p["id"] for p in points],
        }
    ), points


def select_run_and_fxx() -> tuple[datetime, int, datetime]:
    """Find the most recent HRDPS run that's actually published, and the
    forecast hour (fxx) that lands closest to local midday today."""
    now_utc = datetime.now(timezone.utc)
    target_local = datetime.now(LOCAL_TZ).replace(hour=TARGET_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    target_utc = target_local.astimezone(timezone.utc)

    latest_cycle_hour = (now_utc.hour // CYCLE_HOURS) * CYCLE_HOURS
    candidate = now_utc.replace(hour=latest_cycle_hour, minute=0, second=0, microsecond=0)

    for _ in range(MAX_RUN_LOOKBACK):
        fxx = round((target_utc - candidate).total_seconds() / 3600)
        fxx = max(0, min(fxx, MAX_FXX))
        probe = Herbie(candidate.replace(tzinfo=None), model=MODEL, product=PRODUCT, fxx=fxx, variable="TMP", level="AGL-2m", verbose=False)
        if probe.grib is not None:
            valid_time = candidate + timedelta(hours=fxx)
            return candidate.replace(tzinfo=None), fxx, valid_time
        candidate -= timedelta(hours=CYCLE_HOURS)

    sys.exit(f"Could not find a published HRDPS run in the last {MAX_RUN_LOOKBACK * CYCLE_HOURS} hours.")


def fetch_points(run_date: datetime, fxx: int, points_df: pd.DataFrame) -> dict:
    results = {pid: {} for pid in points_df["id"]}
    distances = {}

    for spec in VARIABLES:
        H = Herbie(run_date, model=MODEL, product=PRODUCT, fxx=fxx, variable=spec["variable"], level=spec["level"], verbose=False)
        ds = H.xarray()
        data_var = list(ds.data_vars)[0]
        picked = ds.herbie.pick_points(points_df, method="nearest")

        for i, pid in enumerate(picked["point_id"].values):
            raw_value = float(picked[data_var].values[i])
            results[pid][spec["key"]] = spec["convert"](raw_value)
            distances[pid] = round(float(picked["point_grid_distance"].values[i]), 2)

    for pid in results:
        results[pid]["grid_distance_km"] = distances[pid]

    return results


def main():
    points_df, point_meta = load_active_points()
    run_date, fxx, valid_time = select_run_and_fxx()

    print(f"Using {MODEL} run {run_date:%Y-%m-%d %HZ}, forecast hour {fxx} (valid {valid_time:%Y-%m-%d %H:%M} UTC)")

    values_by_id = fetch_points(run_date, fxx, points_df)

    locations_out = {}
    for p in point_meta:
        locations_out[p["id"]] = {
            "route": p["route"],
            "label": p["label"],
            "lat": p["lat"],
            "lon": p["lon"],
            **values_by_id[p["id"]],
        }

    output = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": MODEL,
        "model_run_utc": run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forecast_valid_utc": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": NOTE,
        "locations": locations_out,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
