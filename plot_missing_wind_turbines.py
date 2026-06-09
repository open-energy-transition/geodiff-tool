#!/usr/bin/env python3
"""
Global poster of wind turbines present in Global Renewable Watch but MISSING in
OpenStreetMap -- made for the #WindHarmonization Mapathon.

Visual style is inspired by open-energy-transition/grid2poster (the
"electric_midnight" theme): a deep-navy night map with a cool glow.

Output: missing-wind-turbines.png  (and .svg)

Run:  python3 plot_missing_wind_turbines.py
"""

import numpy as np
import geopandas as gpd
import geodatasets
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from shapely.geometry import LineString, Polygon

# --------------------------------------------------------------------------- #
# Palette  (grid2poster "electric_midnight")
# --------------------------------------------------------------------------- #
BG        = "#06111F"   # background  / deep navy
LAND      = "#0E2236"   # land fill
LAND_EDGE = "#1D3C5A"   # coastlines / boundaries
GRID      = "#13314C"   # graticule
TXT       = "#EAF4FF"   # primary text
SUBTXT    = "#8FB7D8"   # secondary text
GLOW      = "#7FE0FF"   # turbine glow (cool cyan)
CORE      = "#EAFBFF"   # turbine core (near white)
ACCENT    = "#F6E7A7"   # warm accent (numbers / underline)

ROBINSON = "ESRI:54030"  # beautiful global projection

DATA = "missing-wind-turbines.geojson"
OUT  = "missing-wind-turbines"


def graticule(step_lon=30, step_lat=30, n=180):
    """Lat/lon grid lines as a GeoDataFrame in EPSG:4326."""
    lines = []
    for lon in range(-180, 181, step_lon):
        lines.append(LineString([(lon, lat) for lat in np.linspace(-90, 90, n)]))
    for lat in range(-90, 91, step_lat):
        lines.append(LineString([(lon, lat) for lon in np.linspace(-180, 180, n)]))
    return gpd.GeoDataFrame(geometry=lines, crs="EPSG:4326")


def globe_outline(n=400):
    """The elliptical frame of the world in Robinson, as a polygon (EPSG:4326)."""
    top    = [(lon, 90)  for lon in np.linspace(-180, 180, n)]
    right  = [(180, lat) for lat in np.linspace(90, -90, n)]
    bottom = [(lon, -90) for lon in np.linspace(180, -180, n)]
    left   = [(-180, lat) for lat in np.linspace(-90, 90, n)]
    ring = top + right + bottom + left
    return gpd.GeoDataFrame(geometry=[Polygon(ring)], crs="EPSG:4326")


def main():
    print("Loading data ...")
    pts  = gpd.read_file(DATA).to_crs(ROBINSON)
    land = gpd.read_file(geodatasets.get_path("naturalearth.land")).to_crs(ROBINSON)
    grat = graticule().to_crs(ROBINSON)
    frame = globe_outline().to_crs(ROBINSON)

    n_turbines = len(pts)
    n_countries = pts["COUNTRY"].nunique() if "COUNTRY" in pts.columns else None
    print(f"{n_turbines:,} turbines across {n_countries} countries")

    # ------------------------------------------------------------------ #
    fig, ax = plt.subplots(figsize=(22, 12), dpi=300)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # World frame (subtle dark ellipse) ................................ #
    frame.plot(ax=ax, facecolor="#091A2B", edgecolor=GRID, linewidth=1.0, zorder=0)

    # Graticule ........................................................ #
    grat.plot(ax=ax, color=GRID, linewidth=0.35, alpha=0.55, zorder=1)

    # Land ............................................................. #
    land.plot(ax=ax, facecolor=LAND, edgecolor=LAND_EDGE,
              linewidth=0.4, zorder=2)

    # Turbines -- layered additive glow ................................ #
    # Many points overlap (esp. China) so stacked low-alpha passes build
    # a natural heatmap-like glow in the densest regions.
    glow_passes = [
        dict(s=42, color=GLOW, alpha=0.018),
        dict(s=20, color=GLOW, alpha=0.035),
        dict(s=9,  color=GLOW, alpha=0.07),
        dict(s=3.2, color=CORE, alpha=0.55),
        dict(s=0.7, color=CORE, alpha=0.9),
    ]
    xs = pts.geometry.x.values
    ys = pts.geometry.y.values
    for p in glow_passes:
        ax.scatter(xs, ys, s=p["s"], c=p["color"], alpha=p["alpha"],
                   linewidths=0, edgecolors="none", zorder=3, rasterized=True)

    ax.set_axis_off()
    ax.set_aspect("equal")
    ax.margins(0.01)

    # ------------------------------------------------------------------ #
    # Text / titling
    # ------------------------------------------------------------------ #
    # Prefer a clean sans font if available.
    for cand in ("DejaVu Sans", "Helvetica", "Arial"):
        if any(cand == f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = cand
            break

    fig.text(0.5, 0.965, "New Turbines missing in OpenStreetMap - #WindHarmonization",
             ha="center", va="top", color=TXT, fontsize=40, fontweight="bold")
    # subtle accent underline beneath the title
    fig.add_artist(Line2D([0.43, 0.57], [0.905, 0.905], color=ACCENT,
                          linewidth=1.6, alpha=0.85))

    # Big stats row, bottom-left ....................................... #
    fig.text(0.065, 0.135, f"{n_turbines:,} of 462,133", ha="left", va="bottom",
             color=ACCENT, fontsize=46, fontweight="bold")
    fig.text(0.065, 0.105, "turbines missing in OpenStreetMap", ha="left", va="top",
             color=SUBTXT, fontsize=26)

    # Hashtag, bottom-right ............................................ #

    # Footer credit .................................................... #
    fig.text(0.5, 0.035,
             "Data: Global Renewable Watch · OpenStreetMap · Open Energy Transition · MapYourGrid",
             ha="center", va="bottom", color=SUBTXT, fontsize=11, alpha=0.8)

    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.16)

    print("Saving ...")
    fig.savefig(f"{OUT}.png", dpi=300, facecolor=BG)
    fig.savefig(f"{OUT}.svg", facecolor=BG)
    print(f"Wrote {OUT}.png and {OUT}.svg")


if __name__ == "__main__":
    main()
