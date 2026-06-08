import argparse
import os
import pandas as pd
import geopandas as gpd

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

    # Vectorised point construction (avoids a per-row Python loop).
    geometry = gpd.points_from_xy(df[lon_col], df[lat_col])

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


def load_region(path):
    """
    Load a GeoJSON describing one or more regions and return a single
    (dissolved) geometry in EPSG:3857 to use as a spatial mask.
    """
    print(f"Loading region: {path}")
    region = gpd.read_file(path)

    if region.empty:
        raise ValueError(f"Region file {path} is empty")

    if region.crs is None:
        region = region.set_crs(epsg=4326)

    region = region.to_crs(epsg=3857)

    # Merge all region features into one geometry so a feature counts as
    # "inside" if it falls within any of them.
    return region.geometry.union_all()


def clip_to_region(gdf, region_geom, label):
    """
    Keep only the features of ``gdf`` that fall inside ``region_geom``.
    Uses a spatial-index-backed predicate so it stays fast on large inputs.
    """
    before = len(gdf)
    kept = gdf[gdf.intersects(region_geom)]
    print(f"  {label}: {before} features → {len(kept)} inside region")
    return kept

# ------------------------
# Spatial logic
# ------------------------

def spatial_diff(gdf_a, gdf_b, radius_km, label_a, label_b):
    """
    Return the features of A that lie further than ``radius_km`` from every
    feature of B.

    Instead of buffering all of B and unioning it into one giant geometry
    (O(n) buffers + an expensive union, then a brute-force intersects scan),
    we run a bounded nearest-neighbour join. ``sjoin_nearest`` builds an
    STRtree spatial index over B and resolves every A feature against it in
    vectorised C code, so no buffer or union is ever materialised.
    """
    radius_m = km_to_meters(radius_km)
    print(
        f"Computing diff: {label_a} minus {label_b} "
        f"(radius {radius_km} km)..."
    )

    # Positional index so we can cleanly drop matched rows regardless of the
    # original index's uniqueness.
    gdf_a = gdf_a.reset_index(drop=True)

    # Only geometry is needed from B; max_distance bounds the search radius.
    joined = gpd.sjoin_nearest(
        gdf_a,
        gdf_b[["geometry"]],
        max_distance=radius_m,
        how="inner",
    )
    matched = joined.index.unique()

    kept = gdf_a.drop(index=matched)
    print(
        f"  {label_a}: {len(gdf_a)} features → {len(kept)} kept "
        f"({len(matched)} within {radius_km} km of {label_b})"
    )
    return kept

# ------------------------
# Main
# ------------------------

def main(a_path, b_path, radius_km, out_a, region_path=None):
    print("Loading inputs...")
    gdf_a = load_and_project(a_path)
    gdf_b = load_and_project(b_path)

    if region_path:
        region_geom = load_region(region_path)
        print("Clipping inputs to region...")
        gdf_a = clip_to_region(gdf_a, region_geom, "A")
        gdf_b = clip_to_region(gdf_b, region_geom, "B")

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
    parser = argparse.ArgumentParser(
        description=(
            "Return the features of A that lie further than <radius_km> "
            "from every feature of B."
        )
    )
    parser.add_argument("a_path", help="A input (.geojson or .csv)")
    parser.add_argument("b_path", help="B input (.geojson or .csv)")
    parser.add_argument("radius_km", type=float, help="match radius in km")
    parser.add_argument("out_a", help="output GeoJSON (A minus B)")
    parser.add_argument(
        "--region",
        dest="region_path",
        default=None,
        help=(
            "optional GeoJSON of one or more regions; the diff is only "
            "performed within these regions (features of A and B outside "
            "are ignored)"
        ),
    )
    args = parser.parse_args()

    main(
        args.a_path,
        args.b_path,
        args.radius_km,
        args.out_a,
        region_path=args.region_path,
    )
