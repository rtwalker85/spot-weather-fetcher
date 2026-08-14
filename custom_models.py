"""Herbie model templates for two models Environment Canada moved to a new
server layout that Herbie's bundled templates don't know about yet.

Verified against the real server on 2026-08-14:
- Herbie's built-in `gdps` template points at a URL that now 404s.
- RAQDPS (Canada's real regional air-quality/wildfire-smoke model) has no
  Herbie template at all.

These follow Herbie's own documented extension pattern (a plain class with a
`template(self)` method setting SOURCES) -- see herbie/models/local.py for
the upstream example. register() wires them into Herbie's model registry at
import time so `Herbie(..., model="gdps_new")` / `model="raqdps"` work like
any built-in model.
"""


class gdps_new:
    def template(self):
        self.DESCRIPTION = "GDPS via current WXO-DD path (Herbie's built-in gdps template is stale)"
        self.DETAILS = {
            "current data": "https://dd.weather.gc.ca/{date}/WXO-DD/model_gdps/15km/",
        }
        self.PRODUCTS = {"15km": "global domain, 15km"}
        PATH = (
            f"{self.date:%H}/{self.fxx:03d}/{self.date:%Y%m%dT%HZ}_MSC_GDPS_"
            f"{self.variable}_{self.level}_LatLon0.15_PT{self.fxx:03d}H.grib2"
        )
        self.SOURCES = {
            "msc": f"https://dd.weather.gc.ca/{self.date:%Y%m%d}/WXO-DD/model_gdps/15km/{PATH}"
        }
        self.IDX_SUFFIX = [".grb2.idx", ".idx", ".grib.idx"]
        self.LOCALFILE = f"{self.get_remoteFileName}"


class raqdps:
    def template(self):
        self.DESCRIPTION = "Canada Regional Air Quality Deterministic Prediction System (PM2.5 / wildfire smoke)"
        self.DETAILS = {
            "current data": "https://dd.weather.gc.ca/{date}/WXO-DD/model_raqdps/10km/grib2/",
        }
        self.PRODUCTS = {"10km": "regional domain, 10km"}
        PATH = (
            f"{self.date:%H}/{self.fxx:03d}/{self.date:%Y%m%dT%HZ}_MSC_RAQDPS_"
            f"{self.variable}_{self.level}_RLatLon0.09_PT{self.fxx:03d}H.grib2"
        )
        self.SOURCES = {
            "msc": f"https://dd.weather.gc.ca/{self.date:%Y%m%d}/WXO-DD/model_raqdps/10km/grib2/{PATH}"
        }
        self.IDX_SUFFIX = [".grb2.idx", ".idx", ".grib.idx"]
        self.LOCALFILE = f"{self.get_remoteFileName}"


def register():
    import herbie.models as model_templates

    model_templates.gdps_new = gdps_new
    model_templates.raqdps = raqdps
