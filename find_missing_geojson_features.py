#!/usr/bin/env python3
"""
Find features present in GeoJSON A but missing from GeoJSON B within a radius X meters.

Definition of "missing": a feature in A is considered missing from B when no feature
in B lies within the specified radius of it. The script writes those A features to a
new GeoJSON file.

Efficient approach:
- Reads files with GeoPandas.
- Reprojects both datasets to a metric CRS for meter-based distances.
- Uses GeoPandas/Shapely spatial index instead of pairwise O(n*m) distance checks.
- Uses buffered A geometries only for indexed spatial joining.

Install dependencies:
    pip install geopandas pyogrio shapely pyproj rtree tqdm

Examples:
    python find_missing_geojson_features.py A.geojson B.geojson missing.geojson --radius 25
    python find_missing_geojson_features.py A.geojson B.geojson missing.geojson --radius 50 --crs EPSG:3035

Notes:
- GeoJSON is normally EPSG:4326. Distances in degrees are not valid for meter logic,
  so this script projects geometries before distance matching.
- If no metric CRS is supplied, the script estimates a local UTM CRS from the data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from tqdm.auto import tqdm


GEOMETRY_TYPES_FOR_CENTROID_CRS_ESTIMATE = {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write features from GeoJSON A that are missing in GeoJSON B within a radius in meters."
    )
    parser.add_argument("a_geojson", type=Path, help="Reference GeoJSON containing the expected/full feature set.")
    parser.add_argument("b_geojson", type=Path, help="GeoJSON to check for missing features.")
    parser.add_argument("output_geojson", type=Path, help="Output GeoJSON containing A features missing from B.")
    parser.add_argument(
        "--radius",
        "-r",
        type=float,
        required=True,
        help="Matching radius in meters. A feature is missing if no B feature is within this distance.",
    )
    parser.add_argument(
        "--crs",
        default=None,
        help=(
            "Optional projected CRS for meter-based matching, e.g. EPSG:3035, EPSG:3857, EPSG:32632. "
            "If omitted, a local UTM CRS is estimated automatically."
        ),
    )
    parser.add_argument(
        "--a-id-column",
        default=None,
        help="Optional ID column in A to preserve/check uniqueness. Not required for spatial matching.",
    )
    parser.add_argument(
        "--b-id-column",
        default=None,
        help="Optional ID column in B to preserve/check uniqueness. Not required for spatial matching.",
    )
    parser.add_argument(
        "--engine",
        default="pyogrio",
        choices=["pyogrio", "fiona"],
        help="GeoPandas file IO engine. Default: pyogrio for speed.",
    )
    parser.add_argument(
        "--keep-helper-columns",
        action="store_true",
        help="Keep internal matching helper columns in the output for debugging.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the progress bar.",
    )
    return parser.parse_args()


def validate_inputs(args: argparse.Namespace) -> None:
    if args.radius < 0:
        raise ValueError("--radius must be zero or positive.")
    if not args.a_geojson.exists():
        raise FileNotFoundError(f"A GeoJSON not found: {args.a_geojson}")
    if not args.b_geojson.exists():
        raise FileNotFoundError(f"B GeoJSON not found: {args.b_geojson}")
    if args.a_geojson.resolve() == args.output_geojson.resolve():
        raise ValueError("Output path must not overwrite A input file.")
    if args.b_geojson.resolve() == args.output_geojson.resolve():
        raise ValueError("Output path must not overwrite B input file.")


def read_geojson(path: Path, engine: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, engine=engine)
    if gdf.empty:
        return gdf
    if gdf.geometry.name not in gdf.columns:
        raise ValueError(f"No active geometry column found in {path}")
    gdf = gdf[gdf.geometry.notna()].copy()
    if gdf.empty:
        return gdf
    # Invalid geometries can break buffering/spatial predicates. buffer(0) is a common, fast repair.
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        gdf.loc[invalid, gdf.geometry.name] = gdf.loc[invalid, gdf.geometry.name].buffer(0)
    return gdf


def ensure_crs(gdf: gpd.GeoDataFrame, path: Path) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    if gdf.crs is None:
        # GeoJSON is conventionally WGS84. Use this as a pragmatic default.
        print(f"Warning: {path} has no CRS. Assuming EPSG:4326.", file=sys.stderr)
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def estimate_metric_crs(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> CRS:
    combined = pd.concat(
        [a[[a.geometry.name]].rename(columns={a.geometry.name: "geometry"}),
         b[[b.geometry.name]].rename(columns={b.geometry.name: "geometry"})],
        ignore_index=True,
    )
    combined_gdf = gpd.GeoDataFrame(combined, geometry="geometry", crs=a.crs)

    if combined_gdf.crs != "EPSG:4326":
        lonlat = combined_gdf.to_crs("EPSG:4326")
    else:
        lonlat = combined_gdf

    # GeoPandas estimates a suitable local UTM CRS from bounds.
    estimated = lonlat.estimate_utm_crs()
    if estimated is None:
        raise ValueError(
            "Could not estimate a metric CRS automatically. Provide one with --crs, e.g. --crs EPSG:3035."
        )
    return CRS.from_user_input(estimated)


def prepare_projected(
    a: gpd.GeoDataFrame,
    b: gpd.GeoDataFrame,
    target_crs: CRS,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    if a.crs != b.crs:
        b = b.to_crs(a.crs)
    return a.to_crs(target_crs), b.to_crs(target_crs)


def find_missing_features(
    a_original: gpd.GeoDataFrame,
    a_projected: gpd.GeoDataFrame,
    b_projected: gpd.GeoDataFrame,
    radius_m: float,
    keep_helper_columns: bool = False,
) -> gpd.GeoDataFrame:
    """
    Return rows from a_original whose corresponding projected geometry in A has
    no B geometry within radius_m.
    """
    if a_projected.empty:
        return a_original.copy()

    if b_projected.empty:
        return a_original.copy()

    # Preserve a stable key before spatial operations.
    a_work = a_projected[[a_projected.geometry.name]].copy()
    b_work = b_projected[[b_projected.geometry.name]].copy()
    a_work["__a_rowid"] = a_work.index
    b_work["__b_rowid"] = b_work.index

    # Buffer A by the radius and spatially join against B using the spatial index.
    # predicate="intersects" against the buffer means distance(A, B) <= radius.
    a_buffers = a_work.copy()
    a_buffers["geometry"] = a_buffers.geometry.buffer(radius_m)
    a_buffers = a_buffers.set_geometry("geometry")

    joined = gpd.sjoin(
        a_buffers[["__a_rowid", "geometry"]],
        b_work[["__b_rowid", b_work.geometry.name]],
        how="left",
        predicate="intersects",
    )

    matched_a_ids = joined.loc[joined["__b_rowid"].notna(), "__a_rowid"].unique()
    missing_mask = ~a_original.index.isin(matched_a_ids)
    missing = a_original.loc[missing_mask].copy()

    if keep_helper_columns:
        missing["__missing_reason"] = f"No feature in B within {radius_m:g} meters"

    return missing


def write_geojson(gdf: gpd.GeoDataFrame, output_path: Path, engine: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # GeoJSON output is usually expected in WGS84.
    if not gdf.empty and gdf.crs is not None and CRS.from_user_input(gdf.crs) != CRS.from_epsg(4326):
        gdf = gdf.to_crs("EPSG:4326")
    gdf.to_file(output_path, driver="GeoJSON", engine=engine)


def main() -> int:
    args = parse_args()
    validate_inputs(args)

    progress = tqdm(
        total=6,
        desc="Finding missing features",
        unit="step",
        disable=args.no_progress,
    )

    try:
        progress.set_postfix_str("reading A")
        a = ensure_crs(read_geojson(args.a_geojson, args.engine), args.a_geojson)
        progress.update(1)

        progress.set_postfix_str("reading B")
        b = ensure_crs(read_geojson(args.b_geojson, args.engine), args.b_geojson)
        progress.update(1)

        if args.a_id_column and args.a_id_column not in a.columns:
            raise ValueError(f"--a-id-column '{args.a_id_column}' does not exist in A.")
        if args.b_id_column and args.b_id_column not in b.columns:
            raise ValueError(f"--b-id-column '{args.b_id_column}' does not exist in B.")

        if a.empty:
            progress.set_postfix_str("writing empty output")
            print("A contains no valid geometries. Writing empty output.", file=sys.stderr)
            write_geojson(a, args.output_geojson, args.engine)
            progress.update(progress.total - progress.n)
            return 0

        if b.empty:
            progress.set_postfix_str("writing all A features")
            print("B contains no valid geometries. All A features are missing from B.", file=sys.stderr)
            write_geojson(a, args.output_geojson, args.engine)
            progress.update(progress.total - progress.n)
            return 0

        progress.set_postfix_str("choosing metric CRS")
        target_crs = CRS.from_user_input(args.crs) if args.crs else estimate_metric_crs(a, b)
        if not target_crs.is_projected:
            raise ValueError(f"Matching CRS must be projected/meters-based. Got: {target_crs.to_string()}")
        progress.update(1)

        progress.set_postfix_str("projecting geometries")
        a_projected, b_projected = prepare_projected(a, b, target_crs)
        progress.update(1)

        progress.set_postfix_str("spatial matching")
        missing = find_missing_features(
            a_original=a,
            a_projected=a_projected,
            b_projected=b_projected,
            radius_m=args.radius,
            keep_helper_columns=args.keep_helper_columns,
        )
        progress.update(1)

        progress.set_postfix_str("writing output")
        write_geojson(missing, args.output_geojson, args.engine)
        progress.update(1)

    finally:
        progress.close()

    print(f"A features: {len(a):,}")
    print(f"B features: {len(b):,}")
    print(f"Missing from B within {args.radius:g} m: {len(missing):,}")
    print(f"Output written to: {args.output_geojson}")
    print(f"Distance CRS used: {target_crs.to_string()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
