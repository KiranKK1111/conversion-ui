import warnings
warnings.filterwarnings("ignore")

import geopandas as gpd
import topojson
import fiona
import sys
import os
from tqdm import tqdm


def main():
    if len(sys.argv) < 3:
        print("Usage: python kba_data_conversion.py <gdb_path> <output_file>")
        sys.exit(1)
    file_path = sys.argv[1]
    output = sys.argv[2]

    if not os.path.exists(file_path):
        print(f"Input GDB path does not exist: {file_path}", file=sys.stderr)
        sys.exit(2)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    steps = [
        "Reading GDB",
        "Reprojecting to EPSG:4326",
        "Dissolving by natname",
        "Cleaning geometries",
        "Building TopoJSON",
        "Writing output",
    ]
    bar = tqdm(total=len(steps), desc="KBA → TopoJSON", unit="step", ncols=80)

    try:
        layers = fiona.listlayers(file_path)
        if "polygons" not in layers:
            bar.close()
            print("'polygons' layer not found in the GDB file.", file=sys.stderr)
            sys.exit(3)

        bar.set_postfix_str(steps[0])
        kba = gpd.read_file(file_path, layer="polygons")
        kba_df = kba[["natname", "geometry"]]
        bar.update(1)

        bar.set_postfix_str(steps[1])
        kba_df = kba_df.to_crs("EPSG:4326")
        bar.update(1)

        bar.set_postfix_str(steps[2])
        kba_df = kba_df.dissolve(by="natname", aggfunc="first", as_index=False).reset_index(drop=True)
        bar.update(1)

        bar.set_postfix_str(steps[3])
        kba_df = kba_df[~kba_df.geometry.is_empty & kba_df.geometry.notna()]
        kba_df = kba_df[kba_df.is_valid]
        kba_df["geometry"] = kba_df["geometry"].buffer(0)
        kba_df = kba_df[["natname", "geometry"]]
        bar.update(1)

        bar.set_postfix_str(steps[4])
        kba_gdf = gpd.GeoDataFrame(kba_df, geometry="geometry", crs="EPSG:4326")
        kba_topojson_data = topojson.Topology(kba_gdf).to_json()
        bar.update(1)

        bar.set_postfix_str(steps[5])
        with open(output, "w") as f:
            f.write(kba_topojson_data)
        bar.update(1)

        bar.close()
        size_mb = os.path.getsize(output) / 1_048_576
        print(f"Wrote {output}  ({size_mb:.2f} MB, {len(kba_df):,} features)")
    except Exception as e:
        bar.close()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(99)

if __name__ == "__main__":
    main()