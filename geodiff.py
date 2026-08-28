import argparse
import os
import pandas as pd
import geopandas as gpd

DEFAULT_RADIUS_KM = 1.0

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


# A shapefile is a set of sibling files, not one file. Only the .shp is
# strictly geometry; the rest carry the index, attributes and CRS.
SHAPEFILE_SIDECARS = (".shx", ".dbf", ".prj")


def missing_shapefile_sidecars(path):
    """
    Return the sidecar extensions that are absent next to ``path``.
    Extensions are matched case-insensitively, since shapefiles are often
    shipped with upper-case suffixes.
    """
    stem = os.path.splitext(path)[0]
    return [
        ext
        for ext in SHAPEFILE_SIDECARS
        if not (os.path.exists(stem + ext) or os.path.exists(stem + ext.upper()))
    ]


def prepare_shapefile(path):
    """
    Make a possibly-incomplete shapefile readable and say out loud what is
    degraded, so a partial dataset never loads silently.
    """
    missing = missing_shapefile_sidecars(path)
    if not missing:
        return

    print(f"  incomplete shapefile, missing: {', '.join(missing)}")

    if ".shx" in missing:
        # Without an index GDAL refuses to open the .shp at all. This config
        # option makes it rebuild the index from the geometry records instead.
        os.environ["SHAPE_RESTORE_SHX"] = "YES"
        print("  → rebuilding the .shx index from the geometry records")
    if ".dbf" in missing:
        print("  → no attribute table; geometries load without properties")
    # A missing .prj needs no action here: read_vector reports and fills in
    # the CRS for any file that does not declare one.


def read_vector(path):
    """
    Read any vector format GeoPandas supports, filling in the CRS when the
    file does not declare one.
    """
    print(f"Loading vector file: {path}")

    if os.path.splitext(path)[1].lower() == ".shp":
        prepare_shapefile(path)

    gdf = gpd.read_file(path)

    if gdf.empty:
        raise ValueError(f"{path} is empty")

    if gdf.crs is None:
        print(f"  {path} declares no CRS; assuming EPSG:4326")
        gdf = gdf.set_crs(epsg=4326)

    return gdf


def load_and_project(path):
    """
    Load GeoJSON / Shapefile / CSV and project to EPSG:3857
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".csv":
        gdf = load_csv_as_gdf(path)
    else:
        gdf = read_vector(path)

    return gdf.to_crs(epsg=3857)


def load_region(path):
    """
    Load a vector file describing one or more regions and return a single
    (dissolved) geometry in EPSG:3857 to use as a spatial mask.
    """
    print(f"Loading region: {path}")
    region = read_vector(path).to_crs(epsg=3857)

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
# Output
# ------------------------

def driver_for(path):
    """
    Pick the OGR driver from the output extension so the tool can write a
    shapefile as readily as it reads one. Anything unrecognised stays
    GeoJSON, which is the historical default.
    """
    return {
        ".shp": "ESRI Shapefile",
        ".gpkg": "GPKG",
    }.get(os.path.splitext(path)[1].lower(), "GeoJSON")

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

    driver = driver_for(out_a)
    print(f"Writing {driver} output...")
    a_minus_b.to_file(out_a, driver=driver)

    print("\nDone!")
    print(f"  {out_a}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Return the features of A that lie further than <radius_km> "
            "from every feature of B."
        )
    )
    parser.add_argument("a_path", help="A input (.geojson, .shp, .gpkg or .csv)")
    parser.add_argument("b_path", help="B input (.geojson, .shp, .gpkg or .csv)")
    parser.add_argument(
        "radius_km",
        type=float,
        nargs="?",
        default=DEFAULT_RADIUS_KM,
        help=f"match radius in km (default: {DEFAULT_RADIUS_KM})",
    )
    parser.add_argument(
        "out_a",
        help="output path, A minus B (.geojson, .shp or .gpkg)",
    )
    parser.add_argument(
        "--region",
        dest="region_path",
        default=None,
        help=(
            "optional vector file of one or more regions; the diff is only "
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
