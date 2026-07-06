from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Any
import json
import logging
import urllib.error
import urllib.request


LOGGER = logging.getLogger(__name__)


DEFAULT_GEOLOCATION_URLS = (
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
    "http://ip-api.com/json/?fields=status,message,lat,lon,city,regionName,countryCode,query",
)


@dataclass(frozen=True)
class InternetLocation:
    latitude: float
    longitude: float
    source_url: str
    label: str = ""


def read_internet_location(
    urls: Iterable[str] | None = None,
    *,
    timeout_seconds: float = 4.0,
) -> InternetLocation | None:
    for url in urls or DEFAULT_GEOLOCATION_URLS:
        try:
            payload = _read_json_url(url, timeout_seconds)
            location = parse_internet_location_payload(payload, url)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            LOGGER.debug("Internet geolocation lookup failed for %s", url, exc_info=True)
            continue
        if location is not None:
            return location
    return None


def parse_internet_location_payload(payload: dict[str, Any], source_url: str) -> InternetLocation | None:
    if str(payload.get("status", "")).lower() == "fail":
        return None
    latitude: float | None = None
    longitude: float | None = None
    if "latitude" in payload and "longitude" in payload:
        latitude = _to_float(payload.get("latitude"))
        longitude = _to_float(payload.get("longitude"))
    elif "lat" in payload and "lon" in payload:
        latitude = _to_float(payload.get("lat"))
        longitude = _to_float(payload.get("lon"))
    elif isinstance(payload.get("loc"), str):
        parts = str(payload["loc"]).split(",", maxsplit=1)
        if len(parts) == 2:
            latitude = _to_float(parts[0])
            longitude = _to_float(parts[1])
    if latitude is None or longitude is None:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    label_parts = [
        str(payload.get(key, "")).strip()
        for key in ("city", "region", "regionName", "country_name", "country", "countryCode")
        if str(payload.get(key, "")).strip()
    ]
    return InternetLocation(latitude=latitude, longitude=longitude, source_url=source_url, label=", ".join(label_parts))


def _read_json_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "juara-station/1.0"})
    with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
        data = response.read(65536)
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Internet geolocation response was not a JSON object")
    return payload


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
