import sys
import os
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union
from tqdm import tqdm

tqdm.pandas()

# ------------------------
# Utilities
# ------------------------

def km_to_meters(km):
    return km * 1000.0


def detect_lat_lon_columns(df):
    """
    Try to detect latitude / longitude column names.
    """
    candidates = [
        ("latitude", "longitude"),
        ("lat", "lon"),
        ("lat", "lng"),
    ]

    cols = [c.lower() for c in df.columns]

    for lat, lon in candidates:
        if lat in cols and lon in cols:
            return (
                df.columns[cols.index(lat)],
                df.columns[cols.index(lon)],
            )

    raise ValueError(
        "CSV must contain latitude/longitude columns "
        "(e.g. latitude & longitude, lat & lon, lat & lng)"
    )


def load_csv_as_gdf(path):
    print(f"Loading CSV: {path}")
    df = pd.read_csv(path)

    if df.empty:
        raise ValueError(f"{path} is empty")

    lat_col, lon_col = detect_lat_lon_columns(df)

    geometry = [
        Point(xy)
        for xy in zip(df[lon_col], df[lat_col])
    ]

    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    return gdf


def load_and_project(path):
    """
    Load GeoJSON / Shapefile / CSV and project to EPSG:3857
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        gdf = load_csv_as_gdf(path)
    else:
        print(f"Loading vector file: {path}")
        gdf = gpd.read_file(path)

        if gdf.empty:
            raise ValueError(f"{path} is empty")

        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)

    return gdf.to_crs(epsg=3857)

# ------------------------
# Spatial logic
# ------------------------

def build_buffer_union(gdf, radius_m, label):
    print(f"Buffering geometries for {label}...")
    buffered = [
        geom.buffer(radius_m)
        for geom in tqdm(gdf.geometry, desc=f"Buffering {label}")
    ]
    return unary_union(buffered)


def spatial_diff(gdf_a, gdf_b, radius_km, label_a, label_b):
    radius_m = km_to_meters(radius_km)

    buffered_b_union = build_buffer_union(
        gdf_b, radius_m, label_b
    )

    print(f"Computing diff: {label_a} minus {label_b}...")
    keep_mask = []

    for geom in tqdm(
        gdf_a.geometry,
        desc=f"Comparing {label_a} → {label_b}"
    ):
        keep_mask.append(not geom.intersects(buffered_b_union))

    return gdf_a[keep_mask]

# ------------------------
# Main
# ------------------------

def main(a_path, b_path, radius_km, out_a):
    print("Loading inputs...")
    gdf_a = load_and_project(a_path)
    gdf_b = load_and_project(b_path)

    a_minus_b = spatial_diff(
        gdf_a, gdf_b, radius_km, "A", "B"
    )

    print("Reprojecting output to WGS84...")
    a_minus_b = a_minus_b.to_crs(epsg=4326)

    print("Writing GeoJSON output...")
    a_minus_b.to_file(out_a, driver="GeoJSON")

    print("\nDone!")
    print(f"  {out_a}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(
            "Usage:\n"
            "  python geojson_diff.py <a.(geojson|csv)> <b.(geojson|csv)> "
            "<radius_km> <a_minus_b.geojson>"
        )
        sys.exit(1)

    a_path = sys.argv[1]
    b_path = sys.argv[2]
    radius_km = float(sys.argv[3])
    out_a = sys.argv[4]

    main(a_path, b_path, radius_km, out_a)
