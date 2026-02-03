
# geodiff - A Spatial Difference Tool (GeoJSON / CSV)

This script computes a spatial difference between two geospatial datasets.
It keeps features from dataset **A** that are **not within a given radius** of any feature in dataset **B**.

It supports:

* GeoJSON and other vector formats supported by GeoPandas
* CSV files containing latitude/longitude columns

## Supported Input Formats

### Vector files

* GeoJSON
* Shapefile
* Any format readable by `geopandas.read_file()`

### CSV files

CSV files must contain latitude and longitude columns in WGS84.

Accepted column name pairs:

* `latitude`, `longitude`
* `lat`, `lon`
* `lat`, `lng`

Each row is converted into a Point geometry.

---

## Usage

```bash
python geojson_diff.py <a_input> <b_input> <radius_km> <output.geojson>
```

### Example

```bash
python geojson_diff.py substations.csv substations.geojson 1 result.geojson
```

This keeps points from `substations.csv` that are more than 1 km away from any feature in `cities.geojson`.

---

## Extracting Country Data from OpenStreetMap (Overpass Turbo)

You can use **Overpass Turbo** to extract OpenStreetMap data for a specific country and feature type.

### Example: All electrical substations in India

1. Open: [https://overpass-turbo.eu](https://overpass-turbo.eu)
2. Paste the following query:

```overpass
/*
  All electrical substations in India
*/
[out:json][timeout:180];

// Get the area for India
area["ISO3166-1"="IN"][admin_level=2]->.india;

// Query substations
(
  node["power"="substation"](area.india);
  way["power"="substation"](area.india);
  relation["power"="substation"](area.india);
);

// Output results
out body;
>;
out skel qt;
```

3. Click **Run**
4. When results load:

   * Click **Export**
   * Choose **GeoJSON**
   * Save the file (e.g. `india_substations.geojson`)

This file can be used directly as input to the script.

### Example: All hydro power plants and generators across the globe

```overpass
[out:json][timeout:900];
(
  /* Hydropower plants */
  node["power"="plant"]["plant:source"~"hydro|water"];
  way["power"="plant"]["plant:source"~"hydro|water"];
  relation["power"="plant"]["plant:source"~"hydro|water"];

  /* Explicit generators */
  node["power"="generator"]["generator:source"~"hydro|water"];
  way["power"="generator"]["generator:source"~"hydro|water"];
  relation["power"="generator"]["generator:source"~"hydro|water"];

  /* Waterway-embedded turbines / generators */
  node["generator:method"="water-turbine"];
  way["generator:method"="water-turbine"];
  relation["generator:method"="water-turbine"];

  node["turbine:type"];
  way["turbine:type"];
  relation["turbine:type"];
);
out center tags;

```

### Example: All power plants with battery storage included

```overpass
/*
  Overpass Turbo query — Battery storage POWER PLANTS worldwide
  Includes only power=plant features
  Excludes all power=generator features
*/

[out:json][timeout:900][maxsize:1073741824];

(
  /* power=plant with battery-related source tags */

  // plant:source=battery
  node["power"="plant"]["plant:source"~"(?i)battery"];
  way["power"="plant"]["plant:source"~"(?i)battery"];
  relation["power"="plant"]["plant:source"~"(?i)battery"];

  // generator:source=battery (applied to the plant object itself)
  node["power"="plant"]["generator:source"~"(?i)battery"];
  way["power"="plant"]["generator:source"~"(?i)battery"];
  relation["power"="plant"]["generator:source"~"(?i)battery"];

  // source=battery (generic, but sometimes used)
  node["power"="plant"]["source"~"(?i)battery"];
  way["power"="plant"]["source"~"(?i)battery"];
  relation["power"="plant"]["source"~"(?i)battery"];

  // direct power=battery plant tagging (rare)
  node["power"="battery"];
  way["power"="battery"];
  relation["power"="battery"];
);

out tags center geom;
```
