import json

from shapely.geometry import Point, shape


def load_neighborhoods(geojson_path: str) -> list[tuple[str, object]]:
    """Load SF neighborhood polygons. Returns [(name, polygon), ...]."""
    with open(geojson_path) as f:
        data = json.load(f)
    return [
        (feature["properties"]["name"], shape(feature["geometry"]))
        for feature in data["features"]
    ]


def lookup_neighborhood(lat: float, lon: float, neighborhoods: list[tuple[str, object]]) -> str:
    point = Point(lon, lat)
    for name, polygon in neighborhoods:
        if polygon.contains(point):  # type: ignore[union-attr]
            return name
    return ""


def enrich_with_neighborhoods(
    practices: list[dict],
    geocode_results: dict[str, tuple[float, float]],
    neighborhoods: list[tuple[str, object]],
) -> None:
    """Mutates each practice dict in-place, setting neighborhood."""
    for practice in practices:
        coords = geocode_results.get(practice["npi_number"])
        if coords:
            practice["neighborhood"] = lookup_neighborhood(coords[0], coords[1], neighborhoods)
