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


def _call_census_batch_geocoder(
    csv_content: str, retries: int = 3
) -> dict[str, tuple[float, float] | None]:
    """Submit a batch geocoding request.

    Returns {npi_number: (lat, lon)} for matches and {npi_number: None} for no-matches,
    so both outcomes can be cached and not re-attempted on retry.
    """
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

    results: dict[str, tuple[float, float] | None] = {}
    reader = csv.reader(io.StringIO(response.text))
    for row in reader:
        if not row:
            continue
        npi_number = row[0].strip()
        if len(row) < 3:
            continue
        if row[2].strip() != "Match":
            results[npi_number] = None
            continue
        if len(row) < 6:
            results[npi_number] = None
            continue
        try:
            lon_str, lat_str = row[5].strip().split(",")
            results[npi_number] = (float(lat_str), float(lon_str))
        except ValueError:
            results[npi_number] = None
    return results


def geocode_practices(practices: list[dict], cache_path: str) -> dict[str, tuple[float, float]]:
    """Batch geocode practices via Census Geocoder. Returns {npi_number: (lat, lon)} for matches only."""
    # Cache stores matched coords as [lat, lon] and no-matches as null.
    raw_cache: dict[str, list[float] | None] = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            raw_cache = json.load(f)

    uncached = [p for p in practices if p["npi_number"] not in raw_cache]
    if not uncached:
        print(f"  All {len(raw_cache)} geocoding results loaded from cache.")
    else:
        print(f"  Geocoding {len(uncached)} practices in chunks (cache has {len(raw_cache)})...")
        csv_lines = [
            f'{p["npi_number"]},{_strip_suite_number(p["address_1"])},{p["address_city"]},{p["address_state"]},{p["address_zip"]}'
            for p in uncached
        ]

        chunk_size = 9000  # Census Geocoder limit is 10,000; leave headroom
        new_results: dict[str, tuple[float, float] | None] = {}
        for i in range(0, len(csv_lines), chunk_size):
            chunk = csv_lines[i:i + chunk_size]
            chunk_num = i // chunk_size + 1
            total_chunks = (len(csv_lines) + chunk_size - 1) // chunk_size
            print(f"  Chunk {chunk_num}/{total_chunks} ({len(chunk)} records)...")
            new_results.update(_call_census_batch_geocoder("\n".join(chunk)))

        raw_cache.update(new_results)  # type: ignore[arg-type]
        with open(cache_path, "w") as f:
            json.dump(raw_cache, f)

        matched = sum(1 for v in new_results.values() if v is not None)
        print(f"  Geocoded {len(uncached)}: {matched} matched, {len(uncached) - matched} no-match (cached).")

    return {k: tuple(v) for k, v in raw_cache.items() if v is not None}  # type: ignore[misc]
