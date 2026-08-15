#!/usr/bin/env python3
"""Fetch multi-model point forecasts for tracked trail locations and write latest.json.

Pipeline per model: Herbie downloads the raw GRIB2 file(s) from the
originating agency's public data servers -> cfgrib/xarray decode into a grid
of numbers -> Herbie's pick_points() finds the grid cell nearest each
location in locations.json (using real lat/lon distances, which is what
keeps this correct on rotated grids like HRDPS/RDPS) -> results for every
model are merged into one latest.json, keyed by model then by location.

Different models publish different variables (e.g. only some publish a
direct cloud-ceiling height or freezing level) -- that's expected. A
location's entry simply omits whatever a given model doesn't provide.
"""

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from herbie import Herbie

import custom_models

custom_models.register()

LOCATIONS_FILE = Path(__file__).parent / "locations.json"
OUTPUT_FILE = Path(__file__).parent / "latest.json"

# Each run is aimed at local midday, since that's the window that matters
# for a trail-run report. Actual valid time depends on what each model's
# most recently published run can reach.
LOCAL_TZ = ZoneInfo("America/Edmonton")
TARGET_LOCAL_HOUR = 12

MAX_RUN_LOOKBACK = 4  # how many cycles to step back if the latest run isn't published yet


# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------

def k_to_c(k):
    return round(k - 273.15, 1)


def ms_to_kmh(v):
    return round(v * 3.6, 1)


def m_to_km(v):
    return round(v / 1000, 2)


def kgm3_to_ugm3(v):
    return round(v * 1e9, 1)


def frac_to_pct(v):
    return round(v * 100, 0)


def round0(v):
    return round(v, 0)


def cape_convert(v):
    # CAPE can't physically be negative; models use negative sentinels
    # (e.g. -999, or small negative rounding artifacts) for "no value".
    if v < 0:
        return None
    return round(v, 0)


# ---------------------------------------------------------------------------
# Model registry
#
# fetch_style:
#   "per_variable_file" -- one GRIB2 file per variable (HRDPS/RDPS/GDPS/RAQDPS).
#                           Each variable entry needs herbie variable=/level=.
#   "bundled"           -- one GRIB2 file holds every variable for that hour
#                           (HRRR/RAP/NAM/GFS/IFS/AIFS). Each variable entry
#                           needs a Herbie search string (matched against the
#                           file's own inventory).
#
# wind_uv: for bundled models, the two search strings for the eastward/
# northward wind components at 10m, used to derive speed + direction
# ourselves (kept simple: no dataset-merging, just two point-picks + trig).
# ---------------------------------------------------------------------------

MODELS = [
    {
        "key": "hrdps",
        "label": "HRDPS Continental (Canada, 2.5km)",
        "herbie_model": "hrdps",
        "product": "continental",
        "fetch_style": "per_variable_file",
        "cycle_hours": 6,
        "max_fxx": 48,
        "variables": [
            {"key": "temp_c", "variable": "TMP", "level": "AGL-2m", "convert": k_to_c},
            {"key": "cloud_cover_pct", "variable": "TCDC", "level": "Sfc", "convert": round0},
            {"key": "wind_speed_kmh", "variable": "WIND", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_gust_kmh", "variable": "GUST", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_dir_deg", "variable": "WDIR", "level": "AGL-10m", "convert": round0},
            {"key": "cape_jkg", "variable": "CAPE", "level": "Sfc", "convert": cape_convert},
            {"key": "model_elevation_m", "variable": "HGT", "level": "Sfc", "convert": round0},
        ],
    },
    {
        "key": "rdps",
        "label": "RDPS (Canada, 10km)",
        "herbie_model": "rdps",
        "product": "hrdps",  # Herbie's literal (odd) product name for this model
        "fetch_style": "per_variable_file",
        "cycle_hours": 6,
        "max_fxx": 84,
        "variables": [
            {"key": "temp_c", "variable": "AirTemp", "level": "AGL-2m", "convert": k_to_c},
            {"key": "cloud_cover_pct", "variable": "TotalCloudCover", "level": "Sfc", "convert": round0},
            {"key": "wind_speed_kmh", "variable": "WindSpeed", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_gust_kmh", "variable": "WindGust", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_dir_deg", "variable": "WindDir", "level": "AGL-10m", "convert": round0},
            {"key": "cape_jkg", "variable": "CAPE", "level": "Sfc", "convert": cape_convert},
            # No terrain-height field is published for this model (verified against
            # the real file listing -- GeopotentialHeight is isobaric-levels only).
        ],
    },
    {
        "key": "gdps",
        "label": "GDPS (Canada global, 15km)",
        "herbie_model": "gdps_new",
        "product": "15km",
        "fetch_style": "per_variable_file",
        "cycle_hours": 12,
        "max_fxx": 240,
        "variables": [
            {"key": "temp_c", "variable": "AirTemp", "level": "AGL-2m", "convert": k_to_c},
            {"key": "cloud_cover_pct", "variable": "TotalCloudCover", "level": "Sfc", "convert": round0},
            {"key": "wind_speed_kmh", "variable": "WindSpeed", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_gust_kmh", "variable": "WindGust", "level": "AGL-10m", "convert": ms_to_kmh},
            {"key": "wind_dir_deg", "variable": "WindDir", "level": "AGL-10m", "convert": round0},
            {"key": "cape_jkg", "variable": "CAPE", "level": "Sfc", "convert": cape_convert},
            # No terrain-height field is published for this model either (same check as RDPS).
        ],
    },
    {
        "key": "raqdps",
        "label": "RAQDPS (Canada air quality / wildfire smoke, 10km)",
        "herbie_model": "raqdps",
        "product": "10km",
        "fetch_style": "per_variable_file",
        "cycle_hours": 12,
        "max_fxx": 72,
        "variables": [
            {"key": "pm25_ugm3", "variable": "PM2.5", "level": "Sfc", "convert": kgm3_to_ugm3},
            {"key": "pm25_wildfire_smoke_ugm3", "variable": "PM2.5-WildfireSmokePlume", "level": "Sfc", "convert": kgm3_to_ugm3},
        ],
    },
    {
        "key": "hrrr",
        "label": "HRRR (US, 3km)",
        "herbie_model": "hrrr",
        "product": "sfc",
        "fetch_style": "bundled",
        "cycle_hours": 1,
        "max_fxx": 48,
        "variables": [
            {"key": "temp_c", "search": ":TMP:2 m above ground:", "convert": k_to_c},
            {"key": "cloud_cover_pct", "search": r":TCDC:entire atmosphere[^:]*:(?!.*ave fcst)", "convert": round0},
            {"key": "wind_gust_kmh", "search": ":GUST:surface:", "convert": ms_to_kmh},
            {"key": "visibility_km", "search": ":VIS:surface:", "convert": m_to_km},
            {"key": "cape_jkg", "search": ":CAPE:surface:", "convert": cape_convert},
            {"key": "cloud_ceiling_m", "search": ":HGT:cloud ceiling:", "convert": round0},
            {"key": "freezing_level_m", "search": ":HGT:0C isotherm:", "convert": round0},
            {"key": "model_elevation_m", "search": ":HGT:surface:", "convert": round0},
            {"key": "smoke_ugm3", "search": ":MASSDEN:8 m above ground:", "convert": kgm3_to_ugm3},
            {"key": "precip_1h_mm", "search": ":APCP:surface:", "convert": round0},
        ],
        "wind_uv": r":(UGRD|VGRD):10 m above ground:",
        "precip_type_flags": {
            "rain": r":CRAIN:surface:(?!.*ave fcst)",
            "snow": r":CSNOW:surface:(?!.*ave fcst)",
            "freezing_rain": r":CFRZR:surface:(?!.*ave fcst)",
            "ice_pellets": r":CICEP:surface:(?!.*ave fcst)",
        },
    },
    {
        "key": "rap",
        "label": "RAP (US, 13km)",
        "herbie_model": "rap",
        "product": "awp130pgrb",
        "fetch_style": "bundled",
        "cycle_hours": 1,
        "max_fxx": 21,
        "variables": [
            {"key": "temp_c", "search": ":TMP:2 m above ground:", "convert": k_to_c},
            {"key": "cloud_cover_pct", "search": r":TCDC:entire atmosphere[^:]*:(?!.*ave fcst)", "convert": round0},
            {"key": "wind_gust_kmh", "search": ":GUST:surface:", "convert": ms_to_kmh},
            {"key": "visibility_km", "search": ":VIS:surface:", "convert": m_to_km},
            {"key": "cape_jkg", "search": ":CAPE:surface:", "convert": cape_convert},
            {"key": "cloud_ceiling_m", "search": ":HGT:cloud ceiling:", "convert": round0},
            {"key": "freezing_level_m", "search": ":HGT:0C isotherm:", "convert": round0},
            {"key": "model_elevation_m", "search": ":HGT:surface:", "convert": round0},
            {"key": "smoke_ugm3", "search": ":MASSDEN:8 m above ground:", "convert": kgm3_to_ugm3},
            {"key": "precip_1h_mm", "search": ":APCP:surface:", "convert": round0},
        ],
        "wind_uv": r":(UGRD|VGRD):10 m above ground:",
        "precip_type_flags": {
            "rain": r":CRAIN:surface:(?!.*ave fcst)",
            "snow": r":CSNOW:surface:(?!.*ave fcst)",
            "freezing_rain": r":CFRZR:surface:(?!.*ave fcst)",
            "ice_pellets": r":CICEP:surface:(?!.*ave fcst)",
        },
    },
    {
        "key": "nam",
        "label": "NAM CONUS Nest (US, 5km)",
        "herbie_model": "nam",
        "product": "conusnest.hiresf",
        "fetch_style": "bundled",
        "cycle_hours": 6,
        "max_fxx": 60,
        "variables": [
            {"key": "temp_c", "search": ":TMP:2 m above ground:", "convert": k_to_c},
            {"key": "cloud_cover_pct", "search": r":TCDC:entire atmosphere[^:]*:(?!.*ave fcst)", "convert": round0},
            {"key": "wind_gust_kmh", "search": ":GUST:surface:", "convert": ms_to_kmh},
            {"key": "visibility_km", "search": ":VIS:surface:", "convert": m_to_km},
            {"key": "cape_jkg", "search": ":CAPE:surface:", "convert": cape_convert},
            {"key": "cloud_ceiling_m", "search": ":HGT:cloud ceiling:", "convert": round0},
            {"key": "freezing_level_m", "search": ":HGT:0C isotherm:", "convert": round0},
            {"key": "model_elevation_m", "search": ":HGT:surface:", "convert": round0},
            {"key": "precip_1h_mm", "search": ":APCP:surface:", "convert": round0},
        ],
        "wind_uv": r":(UGRD|VGRD):10 m above ground:",
        "precip_type_flags": {
            "rain": r":CRAIN:surface:(?!.*ave fcst)",
            "snow": r":CSNOW:surface:(?!.*ave fcst)",
            "freezing_rain": r":CFRZR:surface:(?!.*ave fcst)",
            "ice_pellets": r":CICEP:surface:(?!.*ave fcst)",
        },
    },
    {
        "key": "gfs",
        "label": "GFS (US global, 0.25deg)",
        "herbie_model": "gfs",
        "product": "pgrb2.0p25",
        "fetch_style": "bundled",
        "cycle_hours": 6,
        "max_fxx": 240,
        "variables": [
            {"key": "temp_c", "search": ":TMP:2 m above ground:", "convert": k_to_c},
            {"key": "cloud_cover_pct", "search": r":TCDC:entire atmosphere[^:]*:(?!.*ave fcst)", "convert": round0},
            {"key": "wind_gust_kmh", "search": ":GUST:surface:", "convert": ms_to_kmh},
            {"key": "visibility_km", "search": ":VIS:surface:", "convert": m_to_km},
            {"key": "cape_jkg", "search": ":CAPE:surface:", "convert": cape_convert},
            {"key": "cloud_ceiling_m", "search": ":HGT:cloud ceiling:", "convert": round0},
            {"key": "freezing_level_m", "search": ":HGT:0C isotherm:", "convert": round0},
            {"key": "model_elevation_m", "search": ":HGT:surface:", "convert": round0},
            {"key": "precip_1h_mm", "search": ":APCP:surface:", "convert": round0},
        ],
        "wind_uv": r":(UGRD|VGRD):10 m above ground:",
        "precip_type_flags": {
            "rain": r":CRAIN:surface:(?!.*ave fcst)",
            "snow": r":CSNOW:surface:(?!.*ave fcst)",
            "freezing_rain": r":CFRZR:surface:(?!.*ave fcst)",
            "ice_pellets": r":CICEP:surface:(?!.*ave fcst)",
        },
    },
    {
        "key": "ifs",
        "label": "ECMWF IFS (global, 0.25deg)",
        "herbie_model": "ifs",
        "product": "oper",
        "fetch_style": "bundled",
        "cycle_hours": 6,
        "max_fxx": 144,
        "variables": [
            {"key": "temp_c", "search": ":2t:sfc:", "convert": k_to_c},
            {"key": "cloud_cover_pct", "search": ":tcc:sfc:", "convert": frac_to_pct},
            {"key": "cape_jkg", "search": ":mucape:sfc:", "convert": cape_convert},
            {"key": "precip_type_code", "search": ":ptype:sfc:", "convert": round0},
        ],
        "wind_uv": r":(10u|10v):sfc:",
    },
    {
        "key": "aifs",
        "label": "ECMWF AIFS (AI model, global, 0.25deg)",
        "herbie_model": "aifs",
        "product": "oper",
        "fetch_style": "bundled",
        "cycle_hours": 6,
        "max_fxx": 144,
        "variables": [
            {"key": "temp_c", "search": ":2t:sfc:", "convert": k_to_c},
            # Unlike IFS's tcc (a 0-1 fraction), AIFS's tcc is already in percent.
            {"key": "cloud_cover_pct", "search": ":tcc:sfc:", "convert": round0},
        ],
        "wind_uv": r":(10u|10v):sfc:",
    },
]

NOTE = (
    "This file is self-contained -- everything needed to interpret it is "
    "below or inline in each location/model entry; nothing here requires "
    "access to this repo's other files (e.g. locations.json).\n\n"
    "UNITS (encoded in each field's name suffix): _c = Celsius, "
    "_kmh = km/h, wind_dir_deg = degrees true, direction the wind is "
    "coming FROM. _pct = percent. _km = kilometers. _m = meters. "
    "_mm = millimeters (1h accumulation). _ugm3 = micrograms per cubic "
    "meter. cape_jkg = J/kg (convective available potential energy, a "
    "thunderstorm/instability indicator -- 0 is stable, higher is more "
    "unstable).\n\n"
    "grid_distance_km is how far this location actually is from the "
    "nearest grid cell the model sampled -- smaller means that model's "
    "reading is more locally representative of this exact point; larger "
    "(several km or more) means treat that particular model's numbers "
    "for this point with more caution.\n\n"
    "Different models publish different variables -- a location's entry "
    "for a given model only includes what that model actually provides "
    "(e.g. only HRRR/RAP/NAM/GFS publish a direct cloud-ceiling height "
    "and freezing level; HRDPS/RDPS/GDPS do not).\n\n"
    "model_elevation_m is what that model's own terrain grid thinks the "
    "elevation is at the matched point -- compare it against this same "
    "location's own elevation_m field (listed alongside by_model, not a "
    "separate file) to gauge how much a coarse grid may be smoothing "
    "over sharp terrain. A large gap means that model's temperature and "
    "freezing-level readings are likely biased toward its own (usually "
    "lower) elevation, not the real one.\n\n"
    "Each entry under \"models\" has its own run_utc (when that model's "
    "forecast was issued) and forecast_valid_utc (the time the forecast "
    "is actually for) -- these are NOT synchronized across models. Each "
    "model independently grabs its freshest available run and shortest "
    "reasonable lead time, so one model's reading might be for right now "
    "while another's is for several hours from now. Check "
    "forecast_valid_utc per model before treating two models' numbers "
    "for the same location as directly comparable.\n\n"
    "Precipitation TYPE is only included for the US bundled models "
    "(rain/snow/freezing_rain/ice_pellets flags are unambiguous); the "
    "Canadian per-file models' PTYPE-style numeric codes are deferred "
    "until verified against a real code table.\n\n"
    "A null cloud_ceiling_m means the model found no defined cloud base "
    "(effectively clear / unlimited ceiling), not that the reading is "
    "missing. cape_jkg is null when the model reports a negative "
    "sentinel (no instability data) rather than a real value, since CAPE "
    "can't physically be negative.\n\n"
    "This file only includes locations currently tracked as active in "
    "the pipeline's config -- if a route or point you expect isn't "
    "listed under \"locations\" below, it isn't tracked yet; it isn't "
    "that its fetch failed."
)


# ---------------------------------------------------------------------------
# Location loading
# ---------------------------------------------------------------------------

def load_active_points():
    data = json.loads(LOCATIONS_FILE.read_text())
    points = [p for p in data["points"] if p.get("active") and p.get("lat") is not None and p.get("lon") is not None]
    if not points:
        sys.exit("No active locations with coordinates found in locations.json -- nothing to fetch.")
    points_df = pd.DataFrame(
        {
            "latitude": [p["lat"] for p in points],
            "longitude": [p["lon"] for p in points],
            "id": [p["id"] for p in points],
        }
    )
    return points_df, points


# ---------------------------------------------------------------------------
# Run selection (generalized per-model)
# ---------------------------------------------------------------------------

def select_run_and_fxx(model_cfg):
    """Find the most recent run of this model that's actually published, and
    the forecast hour (fxx) that lands closest to local midday today."""
    now_utc = datetime.now(timezone.utc)
    target_local = datetime.now(LOCAL_TZ).replace(hour=TARGET_LOCAL_HOUR, minute=0, second=0, microsecond=0)
    target_utc = target_local.astimezone(timezone.utc)

    cycle_hours = model_cfg["cycle_hours"]
    max_fxx = model_cfg["max_fxx"]

    latest_cycle_hour = (now_utc.hour // cycle_hours) * cycle_hours
    candidate = now_utc.replace(hour=latest_cycle_hour, minute=0, second=0, microsecond=0)

    probe_kwargs = {"model": model_cfg["herbie_model"], "product": model_cfg["product"]}
    if model_cfg["fetch_style"] == "per_variable_file":
        first_var = model_cfg["variables"][0]
        probe_kwargs["variable"] = first_var["variable"]
        probe_kwargs["level"] = first_var["level"]

    for _ in range(MAX_RUN_LOOKBACK):
        fxx = round((target_utc - candidate).total_seconds() / 3600)
        fxx = max(0, min(fxx, max_fxx))
        try:
            probe = Herbie(candidate.replace(tzinfo=None), fxx=fxx, verbose=False, **probe_kwargs)
            found = probe.grib is not None
        except Exception as e:
            print(f"  probe error at {candidate:%Y-%m-%d %HZ}: {e}")
            found = False
        if found:
            valid_time = candidate + timedelta(hours=fxx)
            return candidate.replace(tzinfo=None), fxx, valid_time
        candidate -= timedelta(hours=cycle_hours)

    return None, None, None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def safe_convert(convert_fn, raw):
    """Some fields use NaN as a real, meaningful value (e.g. HRRR's cloud
    ceiling is NaN when no ceiling is defined -- effectively "clear/
    unlimited", not "missing"). NaN isn't valid JSON, so turn it into an
    explicit null rather than emitting a broken NaN token or a fake number."""
    if isinstance(raw, float) and math.isnan(raw):
        return None
    return convert_fn(raw)


def pick_scalar(ds, points_df):
    """Run pick_points and return {point_id: (raw_value, distance_km)}."""
    data_var = list(ds.data_vars)[0]
    picked = ds.herbie.pick_points(points_df, method="nearest")
    out = {}
    for i, pid in enumerate(picked["point_id"].values):
        out[pid] = (float(picked[data_var].values[i]), round(float(picked["point_grid_distance"].values[i]), 2))
    return out


def pick_fields(ds, points_df, field_names):
    """Like pick_scalar, but for a dataset with multiple named variables.
    Returns {point_id: ({field: value, ...}, distance_km)}."""
    picked = ds.herbie.pick_points(points_df, method="nearest")
    out = {}
    for i, pid in enumerate(picked["point_id"].values):
        values = {f: float(picked[f].values[i]) for f in field_names if f in picked}
        out[pid] = (values, round(float(picked["point_grid_distance"].values[i]), 2))
    return out


def fetch_per_variable_model(model_cfg, run_date, fxx, points_df):
    results = {pid: {} for pid in points_df["id"]}
    distances = {}
    for spec in model_cfg["variables"]:
        try:
            H = Herbie(
                run_date, model=model_cfg["herbie_model"], product=model_cfg["product"],
                fxx=fxx, variable=spec["variable"], level=spec["level"], verbose=False,
            )
            ds = H.xarray()
            for pid, (raw, dist) in pick_scalar(ds, points_df).items():
                results[pid][spec["key"]] = safe_convert(spec["convert"], raw)
                distances[pid] = dist
        except Exception as e:
            print(f"  skipping {spec['key']}: {e}")
    for pid in results:
        if pid in distances:
            results[pid]["grid_distance_km"] = distances[pid]
    return results


def fetch_bundled_model(model_cfg, run_date, fxx, points_df):
    results = {pid: {} for pid in points_df["id"]}
    distances = {}

    for spec in model_cfg["variables"]:
        try:
            H = Herbie(run_date, model=model_cfg["herbie_model"], product=model_cfg["product"], fxx=fxx, verbose=False)
            ds = H.xarray(spec["search"])
            for pid, (raw, dist) in pick_scalar(ds, points_df).items():
                results[pid][spec["key"]] = safe_convert(spec["convert"], raw)
                distances[pid] = dist
        except Exception as e:
            print(f"  skipping {spec['key']}: {e}")

    if "wind_uv" in model_cfg:
        try:
            # u10/v10 share a byte range in some models' GRIB2 files (e.g. RAP
            # packs them as sub-messages of one record), which breaks a naive
            # single-field byte-range subset. Fetching both in one combined
            # search avoids that, and lets us reuse Herbie's own with_wind().
            H = Herbie(run_date, model=model_cfg["herbie_model"], product=model_cfg["product"], fxx=fxx, verbose=False)
            ds = H.xarray(model_cfg["wind_uv"]).herbie.with_wind("both")
            for pid, (vals, dist) in pick_fields(ds, points_df, ["si10", "wdir10"]).items():
                if "si10" in vals:
                    results[pid]["wind_speed_kmh"] = ms_to_kmh(vals["si10"])
                if "wdir10" in vals:
                    results[pid]["wind_dir_deg"] = round(vals["wdir10"], 0)
                distances[pid] = dist
        except Exception as e:
            print(f"  skipping wind: {e}")

    if "precip_type_flags" in model_cfg:
        try:
            flag_values = {}
            for label, search in model_cfg["precip_type_flags"].items():
                H = Herbie(run_date, model=model_cfg["herbie_model"], product=model_cfg["product"], fxx=fxx, verbose=False)
                flag_values[label] = pick_scalar(H.xarray(search), points_df)
            for pid in results:
                active = [label for label, vals in flag_values.items() if pid in vals and vals[pid][0] >= 0.5]
                results[pid]["precip_type"] = active[0] if active else "none"
        except Exception as e:
            print(f"  skipping precip_type: {e}")

    for pid in results:
        if pid in distances:
            results[pid]["grid_distance_km"] = distances[pid]
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    points_df, point_meta = load_active_points()

    locations_out = {
        p["id"]: {
            "route": p["route"],
            "label": p["label"],
            "lat": p["lat"],
            "lon": p["lon"],
            "elevation_m": p.get("elevation_m"),
            "by_model": {},
        }
        for p in point_meta
    }
    models_out = {}

    for model_cfg in MODELS:
        key = model_cfg["key"]
        print(f"--- {key} ---")
        try:
            run_date, fxx, valid_time = select_run_and_fxx(model_cfg)
        except Exception as e:
            print(f"  ERROR selecting a run for {key}: {e}")
            models_out[key] = {"label": model_cfg["label"], "status": "error", "error": str(e)}
            continue
        if run_date is None:
            print(f"  could not find a published run in the lookback window, skipping")
            models_out[key] = {"label": model_cfg["label"], "status": "unavailable"}
            continue

        print(f"  run {run_date:%Y-%m-%d %HZ}, fxx {fxx}, valid {valid_time:%Y-%m-%d %H:%M} UTC")
        try:
            if model_cfg["fetch_style"] == "per_variable_file":
                values_by_id = fetch_per_variable_model(model_cfg, run_date, fxx, points_df)
            else:
                values_by_id = fetch_bundled_model(model_cfg, run_date, fxx, points_df)
        except Exception as e:
            print(f"  ERROR fetching {key}: {e}")
            models_out[key] = {"label": model_cfg["label"], "status": "error", "error": str(e)}
            continue

        models_out[key] = {
            "label": model_cfg["label"],
            "status": "ok",
            "run_utc": run_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast_valid_utc": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for pid, values in values_by_id.items():
            locations_out[pid]["by_model"][key] = values

    output = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": NOTE,
        "models": models_out,
        "locations": locations_out,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
