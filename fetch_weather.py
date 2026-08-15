#!/usr/bin/env python3
"""Fetch multi-model, multi-day point forecasts for tracked trail locations
and write latest.json.

WHY THIS USES A POINT API RATHER THAN RAW GRIB2
-----------------------------------------------
Weather models publish GRIB2 files: one variable, one level, one timestep,
for an ENTIRE continent (HRDPS is 3.3M grid points). You cannot subset one
spatially -- compression spans the whole field -- so pulling point forecasts
straight from the source means downloading a continent and discarding
99.9997% of it. Measured on this exact config, that was ~3,389 requests and
~3.9 GB per run to extract ~68,000 numbers.

Services like SpotWx pay that cost once per model run and amortize it across
many users. Open-Meteo does the same thing and exposes it as a free point
API, preserving the one property that matters here: per-model transparency.
Values come back labelled per model (temperature_2m_gem_hrdps_continental,
temperature_2m_gfs_hrrr, ...) so model disagreement stays visible rather
than being blended into one number.

Same coverage, 3 requests instead of 3,389.

CLOUD AT TRAIL ELEVATION ("am I socked in?")
--------------------------------------------
A cloud-ceiling height is ambiguous in the mountains: cloud_cover_low = 100%
at a 2700m ridge could mean socked in, or could mean you're standing in sun
above a valley inversion. So instead of a ceiling, this fetches cloud cover
at several pressure levels plus the geopotential height of each, then
linearly interpolates cloud cover at each location's REAL elevation. That
directly answers whether the crux itself is in cloud.
"""

import json
import sys
import time
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import requests

LOCATIONS_FILE = Path(__file__).parent / "locations.json"
OUTPUT_FILE = Path(__file__).parent / "latest.json"

FORECAST_DAYS = 5
REQUEST_TIMEOUT = 60

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Model ids verified live against the API. Labels mirror how these models are
# normally named (e.g. SpotWx) so the output stays recognisable.
MODELS = {
    "gem_hrdps_continental": "HRDPS Continental (Canada, 2.5km)",
    "gem_regional": "RDPS (Canada regional, 10km)",
    "gem_global": "GDPS (Canada global, 15km)",
    "gfs_hrrr": "HRRR (US, 3km)",
    "gfs_global": "GFS (US global)",
    "ecmwf_ifs025": "ECMWF IFS (global, 0.25deg)",
    "ecmwf_aifs025_single": "ECMWF AIFS (AI model, global, 0.25deg)",
}

# Surface fields. Not every model publishes every one (visibility and
# freezing level are GFS-family only) -- missing ones are simply omitted
# from that model's readings rather than faked.
SURFACE_FIELDS = {
    "temperature_2m": ("temp_c", 1.0),
    "cloud_cover": ("cloud_cover_pct", 1.0),
    "cloud_cover_low": ("cloud_cover_low_pct", 1.0),
    "cloud_cover_mid": ("cloud_cover_mid_pct", 1.0),
    "cloud_cover_high": ("cloud_cover_high_pct", 1.0),
    "wind_speed_10m": ("wind_speed_kmh", 1.0),
    "wind_gusts_10m": ("wind_gust_kmh", 1.0),
    "wind_direction_10m": ("wind_dir_deg", 1.0),
    "precipitation": ("precip_mm", 1.0),
    "snowfall": ("snowfall_cm", 1.0),
    "cape": ("cape_jkg", 1.0),
    "freezing_level_height": ("freezing_level_m", 1.0),
    "visibility": ("visibility_km", 0.001),  # metres -> km
}

# Pressure levels used to interpolate cloud cover at trail elevation.
# Roughly: 900hPa ~1000m, 850 ~1500m, 800 ~2030m, 750 ~2560m, 700 ~3120m --
# which brackets every tracked point (lowest ~1300m, highest ~2790m).
PRESSURE_LEVELS = [900, 850, 800, 750, 700]
# ECMWF IFS/AIFS don't expose pressure-level cloud on this API (verified).
PRESSURE_LEVEL_MODELS = [
    "gem_hrdps_continental",
    "gem_regional",
    "gem_global",
    "gfs_hrrr",
    "gfs_global",
]

AIR_QUALITY_FIELDS = {
    "pm2_5": "pm2_5_ugm3",
    "pm10": "pm10_ugm3",
    "us_aqi": "us_aqi",
    "aerosol_optical_depth": "aerosol_optical_depth",
}

NOTE = (
    "This file is self-contained -- everything needed to interpret it is "
    "below or inline in each location/model entry; nothing here requires "
    "access to this repo's other files (e.g. locations.json).\n\n"
    "FORECAST SERIES, NOT A SNAPSHOT: every model provides hourly readings "
    "covering up to 5 days ahead, in each location's "
    "by_model.<model>.readings array (chronological, one entry per hour). "
    "Models differ in how far ahead they forecast at all -- HRRR and HRDPS "
    "are short-range (roughly 2 days) but highest resolution and best for "
    "next-day planning; GFS, GDPS and ECMWF reach the full 5 days at "
    "coarser resolution. A model's series simply ends when its forecast "
    "does. Check checkpoint_count / earliest_valid_utc / latest_valid_utc "
    "under \"models\" for each model's actual coverage.\n\n"
    "USING THIS FOR TRIP PLANNING -- the models are deliberately kept "
    "separate rather than blended, because the SPREAD BETWEEN THEM IS "
    "ITSELF THE SIGNAL. When models agree closely, the forecast is "
    "confident; when they disagree (say cloud-at-elevation ranging 15-55 "
    "percent, or temperature spanning 3+ degrees), that is genuine "
    "forecast uncertainty and should be surfaced as such rather than "
    "averaged into a single number that hides it. For a go/no-go call, "
    "prefer reporting the range, or the worst case, over the mean.\n\n"
    "WHICH MODELS TO WEIGHT, BY HOW FAR OUT: for today and tomorrow, "
    "prefer HRDPS (2.5km) and HRRR (3km) -- they have much finer terrain "
    "resolution, which matters enormously in steep alpine country, and "
    "they are what a human would check for next-day planning. They only "
    "reach ~2 days out. For days 3-5, only GDPS, GFS and the ECMWF "
    "models still have data; treat those as a coarser trend (is a system "
    "moving in, is the freezing level rising or falling) rather than as "
    "hour-specific detail, and expect them to be revised as the date "
    "approaches.\n\n"
    "ROUTES CAN HAVE SEVERAL TRACKED POINTS: locations each carry a "
    "\"route\" field, and more than one location can share it -- route "
    "\"Rockwall\" has four passes and \"Buller Pass Loop\" has two, because "
    "conditions differ meaningfully along a long traverse. For a "
    "route-level verdict, gate on the WORST point on that route, not an "
    "average of them: being clear at one pass is no comfort if another "
    "is socked in or above the freezing level.\n\n"
    "CLOUD AT TRAIL ELEVATION -- read this before judging whether a route "
    "is socked in. cloud_cover_at_elevation_pct is the important one: cloud "
    "cover interpolated to the location's REAL elevation, so it answers "
    "'is this specific ridge/pass in cloud right now'. Prefer it over the "
    "others for go/no-go calls. The plain cloud_cover_pct (whole-column) "
    "and cloud_cover_low/mid/high_pct fields are ambiguous in mountains: "
    "cloud_cover_low_pct = 100 at a 2700m ridge can mean socked in, OR can "
    "mean the point is in sunshine above a valley inversion. Comparing "
    "cloud_cover_at_elevation_pct against cloud_cover_low_pct distinguishes "
    "those two cases. Not every model publishes pressure-level cloud "
    "(ECMWF IFS/AIFS do not), so that field is absent for those models.\n\n"
    "UNITS (encoded in each field's name suffix): _c = Celsius. _kmh = "
    "km/h. wind_dir_deg = degrees true, the direction wind blows FROM. "
    "_pct = percent. _km = kilometres. _m = metres. _mm = millimetres "
    "(precipitation in that hour). _cm = centimetres (snowfall in that "
    "hour). _ugm3 = micrograms per cubic metre. cape_jkg = J/kg "
    "(instability / thunderstorm potential; 0 is stable, higher is more "
    "unstable). us_aqi is the US Air Quality Index scale. "
    "aerosol_optical_depth is unitless and is a useful wildfire-smoke "
    "haze indicator.\n\n"
    "Different models publish different variables -- a model's readings "
    "only include what that model actually provides. visibility_km and "
    "freezing_level_m are GFS-family only. Air-quality fields (pm2_5, "
    "pm10, us_aqi, aerosol_optical_depth) come from a separate global "
    "forecast (CAMS) and are therefore listed ONCE per location under "
    "air_quality, not repeated per weather model. Check the top-level "
    "air_quality_status field: if it is not \"ok\", that upstream "
    "service failed this run and the air_quality blocks are absent -- "
    "that is a temporary fetch failure, NOT a reading of clean air.\n\n"
    "forecast_elevation_m and grid_distance_km sit at the LOCATION level "
    "(not per model) because every model for a location is resolved to the "
    "same downscaled grid point: forecast_elevation_m is the elevation the "
    "forecast was resolved at, and grid_distance_km is how far that point "
    "sits from the requested coordinates. Compare forecast_elevation_m "
    "against this location's own elevation_m to judge how well the terrain "
    "was captured -- a large gap means temperature and freezing-level "
    "values are biased toward the resolved elevation rather than the real "
    "one. In practice these agree closely (tens of metres) even on sharp "
    "ridges.\n\n"
    "Values are downscaled and elevation-corrected rather than being raw "
    "model grid output. In steep terrain this is materially more accurate "
    "for a specific point than the underlying coarse grid would be.\n\n"
    "This file only includes locations currently tracked as active in the "
    "pipeline's config -- if a route or point you expect isn't listed "
    "under \"locations\" below, it isn't tracked yet; that is not a fetch "
    "failure."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return round(2 * r * asin(min(1.0, sqrt(a))), 2)


def load_active_points():
    data = json.loads(LOCATIONS_FILE.read_text())
    points = [
        p for p in data["points"]
        if p.get("active") and p.get("lat") is not None and p.get("lon") is not None
    ]
    if not points:
        sys.exit("No active locations with coordinates found in locations.json -- nothing to fetch.")
    return points


def as_list(payload):
    """Open-Meteo returns a bare object for one location, a list for many."""
    return payload if isinstance(payload, list) else [payload]


def get_series(hourly, base, model=None):
    """Fetch one variable's series. Open-Meteo suffixes keys with the model
    id when several models are requested, and omits the suffix when only
    one is -- handle both."""
    if model is not None:
        val = hourly.get(f"{base}_{model}")
        if val is not None:
            return val
    return hourly.get(base)


def has_data(series):
    return bool(series) and any(v is not None for v in series)


def interp_cloud_at_elevation(levels, elevation_m):
    """levels: list of (height_m, cloud_pct) for one hour. Linearly
    interpolate cloud cover at elevation_m; clamp to the nearest level when
    the point sits outside the sampled range."""
    pairs = sorted((h, c) for h, c in levels if h is not None and c is not None)
    if not pairs:
        return None
    if elevation_m <= pairs[0][0]:
        return round(float(pairs[0][1]))
    if elevation_m >= pairs[-1][0]:
        return round(float(pairs[-1][1]))
    for (h_lo, c_lo), (h_hi, c_hi) in zip(pairs, pairs[1:]):
        if h_lo <= elevation_m <= h_hi:
            span = h_hi - h_lo
            if span <= 0:
                return round(float(c_lo))
            frac = (elevation_m - h_lo) / span
            return round(float(c_lo + (c_hi - c_lo) * frac))
    return None


def fetch_json(url, params, attempts=3):
    """Transient timeouts from the API do happen (observed on GitHub's
    runners). Retry with backoff rather than losing a whole category of
    data for the hour because one request was slow."""
    last = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last = e
            if attempt < attempts - 1:
                wait = 2 * (attempt + 1)
                print(f"      attempt {attempt + 1} failed ({e}); retrying in {wait}s")
                time.sleep(wait)
    raise last


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    points = load_active_points()
    lats = ",".join(str(p["lat"]) for p in points)
    lons = ",".join(str(p["lon"]) for p in points)
    common = {
        "latitude": lats,
        "longitude": lons,
        "forecast_days": FORECAST_DAYS,
        "timezone": "UTC",
    }

    print(f"Fetching {len(points)} locations x {len(MODELS)} models, {FORECAST_DAYS} days hourly...")

    print("  1/3 surface fields")
    surface = as_list(fetch_json(WEATHER_URL, {
        **common,
        "hourly": ",".join(SURFACE_FIELDS),
        "models": ",".join(MODELS),
    }))

    print("  2/3 pressure-level cloud (for cloud-at-elevation)")
    pl_vars = [f"cloud_cover_{lv}hPa" for lv in PRESSURE_LEVELS]
    pl_vars += [f"geopotential_height_{lv}hPa" for lv in PRESSURE_LEVELS]
    pressure = as_list(fetch_json(WEATHER_URL, {
        **common,
        "hourly": ",".join(pl_vars),
        "models": ",".join(PRESSURE_LEVEL_MODELS),
    }))

    print("  3/3 air quality (PM2.5 / smoke)")
    try:
        air = as_list(fetch_json(AIR_QUALITY_URL, {
            **common,
            "hourly": ",".join(AIR_QUALITY_FIELDS),
        }))
        air_status = "ok"
    except Exception as e:
        print(f"      air quality unavailable after retries: {e}")
        air = [None] * len(points)
        air_status = f"unavailable: {e.__class__.__name__}"

    locations_out = {}
    model_coverage = {key: [] for key in MODELS}

    for idx, point in enumerate(points):
        loc_surface = surface[idx]
        loc_pressure = pressure[idx] if idx < len(pressure) else {}
        loc_air = air[idx] if idx < len(air) else None

        entry = {
            "route": point["route"],
            "label": point["label"],
            "lat": point["lat"],
            "lon": point["lon"],
            "elevation_m": point.get("elevation_m"),
            "by_model": {},
        }

        # The API resolves each location to one downscaled grid point and
        # returns a single elevation/lat/lon for it -- shared by every
        # model, so these belong at the location level, not repeated per
        # model (which would falsely imply per-model terrain).
        forecast_elev = loc_surface.get("elevation")
        if forecast_elev is not None:
            entry["forecast_elevation_m"] = forecast_elev
        g_lat, g_lon = loc_surface.get("latitude"), loc_surface.get("longitude")
        if g_lat is not None and g_lon is not None:
            entry["grid_distance_km"] = haversine_km(point["lat"], point["lon"], g_lat, g_lon)

        s_hourly = loc_surface.get("hourly", {})
        times = s_hourly.get("time", [])
        p_hourly = loc_pressure.get("hourly", {}) if loc_pressure else {}
        elevation = point.get("elevation_m") or loc_surface.get("elevation")

        for model_key in MODELS:
            # Which surface fields this model actually returned.
            present = {}
            for api_name, (out_name, scale) in SURFACE_FIELDS.items():
                series = get_series(s_hourly, api_name, model_key)
                if has_data(series):
                    present[out_name] = (series, scale)
            if not present:
                continue

            # Pre-compute cloud-at-elevation for every hour, when available.
            cloud_at_elev = None
            if model_key in PRESSURE_LEVEL_MODELS and elevation is not None:
                heights = {
                    lv: get_series(p_hourly, f"geopotential_height_{lv}hPa", model_key)
                    for lv in PRESSURE_LEVELS
                }
                clouds = {
                    lv: get_series(p_hourly, f"cloud_cover_{lv}hPa", model_key)
                    for lv in PRESSURE_LEVELS
                }
                if any(has_data(v) for v in heights.values()):
                    cloud_at_elev = []
                    for i in range(len(times)):
                        per_level = []
                        for lv in PRESSURE_LEVELS:
                            h_series, c_series = heights.get(lv), clouds.get(lv)
                            h = h_series[i] if h_series and i < len(h_series) else None
                            c = c_series[i] if c_series and i < len(c_series) else None
                            per_level.append((h, c))
                        cloud_at_elev.append(interp_cloud_at_elevation(per_level, elevation))

            readings = []
            for i, t in enumerate(times):
                reading = {"valid_time_utc": f"{t}Z" if not t.endswith("Z") else t}
                if cloud_at_elev is not None and cloud_at_elev[i] is not None:
                    reading["cloud_cover_at_elevation_pct"] = cloud_at_elev[i]
                for out_name, (series, scale) in present.items():
                    val = series[i] if i < len(series) else None
                    if val is None:
                        continue
                    reading[out_name] = round(val * scale, 2) if scale != 1.0 else val
                # Only keep hours that actually carry data.
                if len(reading) > 1:
                    readings.append(reading)

            if not readings:
                continue

            entry["by_model"][model_key] = {"readings": readings}
            model_coverage[model_key].append((readings[0]["valid_time_utc"], readings[-1]["valid_time_utc"], len(readings)))

        if loc_air:
            a_hourly = loc_air.get("hourly", {})
            a_times = a_hourly.get("time", [])
            aq_readings = []
            for i, t in enumerate(a_times):
                reading = {"valid_time_utc": f"{t}Z" if not t.endswith("Z") else t}
                for api_name, out_name in AIR_QUALITY_FIELDS.items():
                    series = a_hourly.get(api_name)
                    val = series[i] if series and i < len(series) else None
                    if val is not None:
                        reading[out_name] = val
                if len(reading) > 1:
                    aq_readings.append(reading)
            if aq_readings:
                entry["air_quality"] = {
                    "source": "CAMS global air quality forecast (Open-Meteo)",
                    "readings": aq_readings,
                }

        locations_out[point["id"]] = entry

    models_out = {}
    for model_key, label in MODELS.items():
        cov = model_coverage[model_key]
        if not cov:
            models_out[model_key] = {"label": label, "status": "unavailable"}
            continue
        models_out[model_key] = {
            "label": label,
            "status": "ok",
            "earliest_valid_utc": min(c[0] for c in cov),
            "latest_valid_utc": max(c[1] for c in cov),
            "checkpoint_count": max(c[2] for c in cov),
        }
        print(f"  {model_key:24s} {models_out[model_key]['checkpoint_count']:3d} hourly readings "
              f"-> {models_out[model_key]['latest_valid_utc']}")

    output = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Open-Meteo (per-model point forecasts; CAMS for air quality)",
        "note": NOTE,
        "air_quality_status": air_status,
        "models": models_out,
        "locations": locations_out,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
