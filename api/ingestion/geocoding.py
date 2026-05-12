import csv
import io
import json
import os
import re

from http_utils import http_post_with_retry


_SUITE_RE = re.compile(r'\b(ste|suite|unit|apt|#)\s*\S+$', re.IGNORECASE)
_CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"


def _strip_suite_number(address: str) -> str:
    return _SUITE_RE.sub('', address).strip()


def _call_census_batch_geocoder(csv_content: str, retries: int = 3) -> dict[str, tuple[float, float]]:
    """Submit a batch geocoding request. Returns {npi_number: (lat, lon)} for matched records."""
    response = http_post_with_retry(
        _CENSUS_GEOCODER_URL,
        retries=retries,
        timeout=300.0,
        data={
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "returntype": "geographies",
        },
        files={"addressFile": ("batch.csv", csv_content.encode(), "text/csv")},
    )

    results: dict[str, tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(response.text))
    for row in reader:
        if len(row) < 7 or row[2].strip() != "Match":
            continue
        try:
            lon_str, lat_str = row[5].strip().split(",")
            results[row[0].strip()] = (float(lat_str), float(lon_str))
        except ValueError:
            continue
    return results


def geocode_practices(practices: list[dict], cache_path: str) -> dict[str, tuple[float, float]]:
    """Batch geocode practices via Census Geocoder. Returns {npi_number: (lat, lon)}."""
    cache: dict[str, tuple[float, float]] = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            raw = json.load(f)
        cache = {k: tuple(v) for k, v in raw.items()}  # type: ignore[assignment]

    uncached = [p for p in practices if p["npi_number"] not in cache]
    if not uncached:
        print(f"  All {len(cache)} geocoding results loaded from cache.")
        return cache

    print(f"  Geocoding {len(uncached)} practices (cache has {len(cache)})...")
    csv_lines = [
        f'{p["npi_number"]},{_strip_suite_number(p["address_1"])},{p["address_city"]},{p["address_state"]},{p["address_zip"]}'
        for p in uncached
    ]
    new_results = _call_census_batch_geocoder("\n".join(csv_lines))
    cache.update(new_results)

    with open(cache_path, "w") as f:
        json.dump(cache, f)

    matched = sum(1 for p in uncached if p["npi_number"] in new_results)
    print(f"  Geocoded {len(uncached)}: {matched} matched, {len(uncached) - matched} unmatched.")
    return cache
