"""
Aqueduct GDB to TopoJSON Converter
====================================
Converts any layer/indicator from an Aqueduct GDB file to a TopoJSON file
in the same format as bws_annual.topojson.

Usage:
    python aqueduct_gdb_to_topojson.py --gdb <path_to.gdb> --indicator <prefix> [options]

Examples:
    # baseline annual - bws indicator
    python aqueduct_gdb_to_topojson.py --gdb Aq40_Y2023D07M05.gdb --indicator bws

    # baseline annual - bwd indicator, custom output path
    python aqueduct_gdb_to_topojson.py --gdb Aq40_Y2023D07M05.gdb --indicator bwd --output ./bwd_annual.topojson

    # baseline monthly - bws for January (month 01)
    python aqueduct_gdb_to_topojson.py --gdb Aq40_Y2023D07M05.gdb --layer baseline_monthly --indicator bws_01

    # future annual - bau30_ba indicator
    python aqueduct_gdb_to_topojson.py --gdb Aq40_Y2023D07M05.gdb --layer future_annual --indicator bau30_ba

    # List all available layers and indicators in the GDB
    python aqueduct_gdb_to_topojson.py --gdb Aq40_Y2023D07M05.gdb --list

Arguments:
    --gdb          Path to the Aqueduct .gdb file (required)
    --indicator    Indicator column prefix to extract, e.g. bws, bwd, bws_01 (required unless --list)
    --layer        GDB layer name (default: baseline_annual)
    --output       Output .topojson file path (default: auto-generated from indicator and layer names)
    --quantize     Quantization level for TopoJSON (default: 100000)
    --dissolve-by  Column name to dissolve/merge geometries by (e.g. bws_label)
    --simplify     Geometry simplification tolerance in CRS units (e.g. 500 for
                   ~500m in EPSG:3857). Applied before TopoJSON build. Strongly
                   recommended when using --dissolve-by on large datasets.
    --list         List all layers and available indicators in the GDB, then exit
    --log          Path to log file (optional)
"""

import argparse
import gc
import os
import sys
import re
import json
import logging
import warnings

# Suppress harmless warnings that would break the tqdm progress bar display
warnings.filterwarnings("ignore", message=".*organizePolygons.*", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*GeoSeries.notna.*", category=UserWarning)

import fiona
import geopandas as gpd
import numpy as np
import topojson
import topojson.ops
import topojson.core.cut
from tqdm import tqdm


# Memory-efficient replacement for topojson.ops.np_array_from_lists.
#
# Two peak-memory wins over the upstream code path:
#
# 1. np.array(list(zip_longest(*nested_lists, ...))) materializes a Python list
#    of tuples ~4x the final numpy array. Pre-allocating the target array and
#    filling it row-by-row keeps peak memory at output size.
#
# 2. `topojson.core.cut._cutter` does `bk_array.astype(float)` on the result.
#    Our result is already float64, so the copy is redundant but still doubles
#    peak RAM for a few seconds. Returning a subclass whose `.astype(float)`
#    returns self (when dtypes match) skips that copy.
class _NoCopyFloatArray(np.ndarray):
    def astype(self, dtype, *args, **kwargs):
        try:
            if self.dtype == np.dtype(dtype):
                return np.ndarray.view(self, np.ndarray)
        except TypeError:
            pass
        return super().astype(dtype, *args, **kwargs)


def _efficient_np_array_from_lists(nested_lists):
    if not nested_lists:
        return np.empty((0, 0)).view(_NoCopyFloatArray)
    n_rows = len(nested_lists)
    max_len = max((len(r) for r in nested_lists), default=0)
    if max_len == 0:
        return np.empty((n_rows, 0)).view(_NoCopyFloatArray)
    arr = np.full((n_rows, max_len), np.nan, dtype=np.float64)
    for i, row in enumerate(nested_lists):
        if row:
            arr[i, :len(row)] = row
    return arr.view(_NoCopyFloatArray)


topojson.ops.np_array_from_lists = _efficient_np_array_from_lists
topojson.core.cut.np_array_from_lists = _efficient_np_array_from_lists


# ── helpers ──────────────────────────────────────────────────────────────────

class _TqdmStream(logging.StreamHandler):
    """Logging handler that writes via tqdm.write() to avoid breaking the progress bar."""
    def emit(self, record):
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(log_path=None):
    handlers = [_TqdmStream()]
    if log_path:
        handlers.append(logging.FileHandler(log_path))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def list_layers_and_indicators(gdb_path):
    """Print all layers and their available indicator prefixes."""
    layers = fiona.listlayers(gdb_path)
    print(f"\nGDB: {gdb_path}")
    print(f"Layers ({len(layers)}):")
    for layer in layers:
        with fiona.open(gdb_path, layer=layer) as src:
            all_cols = list(src.schema["properties"].keys())
            feature_count = len(src)
        indicators = detect_indicators(all_cols)
        print(f"\n  Layer: {layer}  ({feature_count:,} features)")
        print(f"  All columns: {all_cols}")
        print(f"  Detected indicator prefixes: {sorted(indicators)}")


def detect_indicators(columns):
    """
    Detect indicator prefixes from the list of column names.
    A prefix is any common stem of columns ending in _raw / _score / _cat / _label
    or _r / _l (future annual pattern).
    """
    prefixes = set()
    # Pattern: prefix_raw, prefix_score, prefix_cat, prefix_label
    for col in columns:
        m = re.match(r"^(.+)_(raw|score|cat|label)$", col)
        if m:
            prefixes.add(m.group(1))
        # Future annual uses _r / _l
        m2 = re.match(r"^(.+)_(r|l)$", col)
        if m2:
            prefixes.add(m2.group(1))
    return prefixes


def get_indicator_columns(all_cols, indicator):
    """
    Return the list of columns that belong to the given indicator prefix.
    Matches: {indicator}_raw, {indicator}_score, {indicator}_cat, {indicator}_label
             {indicator}_r, {indicator}_l  (future annual)
    """
    pattern = re.compile(
        r"^" + re.escape(indicator) + r"_(raw|score|cat|label|r|l)$"
    )
    matched = [c for c in all_cols if pattern.match(c)]
    if not matched:
        raise ValueError(
            f"No columns found for indicator '{indicator}'. "
            f"Available columns: {all_cols}"
        )
    return matched


def build_output_path(output_arg, indicator, layer):
    if output_arg:
        return output_arg
    safe_indicator = indicator.replace(".", "_")
    safe_layer = layer.replace(".", "_")
    return f"{safe_indicator}_{safe_layer}.topojson"


# ── chunked topology (memory-bound machines) ──────────────────────────────────
#
# Topology computation materializes large numpy arrays whose peak RAM scales
# with total coord count. For large datasets on memory-constrained machines we
# split the input, build a topology per chunk (arc sharing preserved WITHIN a
# chunk), and merge. Arcs across chunk boundaries aren't deduplicated, so the
# merged file is slightly larger than a single-pass result — but the script
# actually completes instead of OOMing.


def _shift_arc_indices(arcs, offset):
    """Shift every arc index in an arbitrarily nested list by `offset`.
    TopoJSON uses negative indices (~i) to mean reversed arc i, so negatives
    shift the opposite direction."""
    if isinstance(arcs, int):
        return arcs + offset if arcs >= 0 else arcs - offset
    return [_shift_arc_indices(x, offset) for x in arcs]


def _remap_geom_arcs_recursive(geom, offset):
    if "arcs" in geom:
        geom["arcs"] = _shift_arc_indices(geom["arcs"], offset)
    for sub in geom.get("geometries", []):
        _remap_geom_arcs_recursive(sub, offset)


def _reencode_arc(arc, src_scale, src_translate, dst_kx, dst_ky, dst_tx, dst_ty):
    """Re-encode a delta-int arc from one (scale, translate) to another.
    Walks deltas to absolute source-space ints, maps back to floats via the
    source transform, then quantizes to the destination integer grid and
    re-delta-encodes."""
    sx, sy = src_scale
    tx, ty = src_translate
    out = []
    ax = ay = 0  # running absolute in source delta space
    px = py = 0  # running absolute in destination delta space
    for pt in arc:
        ax += pt[0]
        ay += pt[1]
        fx = ax * sx + tx
        fy = ay * sy + ty
        qx = round((fx - dst_tx) * dst_kx)
        qy = round((fy - dst_ty) * dst_ky)
        out.append([qx - px, qy - py])
        px, py = qx, qy
    return out


def _worker_build_chunk(argv):
    """Worker entry: build topology for one chunk and write it to disk.

    argv: [gdf_pickle_path, chunk_i, chunks_n, quantize, topology_int, output_path]
    This path is invoked via `python this_script.py --_worker ...` and exits
    when finished so the OS reclaims all memory before the next chunk runs.
    """
    import pickle
    pickle_path, chunk_i, chunks_n, quantize, topology_int, output_path = argv
    chunk_i = int(chunk_i)
    chunks_n = int(chunks_n)
    quantize = int(quantize)
    topology_enabled = bool(int(topology_int))

    with open(pickle_path, "rb") as f:
        gdf = pickle.load(f)

    n = len(gdf)
    chunk_size = (n + chunks_n - 1) // chunks_n
    start = chunk_i * chunk_size
    end = min(n, start + chunk_size)
    chunk = gdf.iloc[start:end].reset_index(drop=True)

    t = topojson.Topology(chunk, prequantize=quantize, topology=topology_enabled)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(t.to_json())


def build_topojson_chunked(gdf_out, quantize, chunks, topology_enabled=True, persistent_tmpdir=None):
    """Build TopoJSON by spawning one subprocess per chunk, then merging.

    Subprocess isolation is required on memory-constrained machines because
    Python on Windows does not reliably return freed memory to the OS, and
    `topojson.core.cut._cutter` calls `.astype(float)` which temporarily
    doubles peak RAM per chunk. Running each chunk in a fresh interpreter
    keeps the high-water mark bounded by a single chunk's needs.

    If `persistent_tmpdir` is set, chunk outputs are written there and reused
    on re-run — completed chunks are skipped. This lets you resume after a
    failure without redoing the successful chunks.
    """
    import subprocess
    import tempfile
    import pickle
    import shutil

    minx, miny, maxx, maxy = gdf_out.total_bounds
    span_x = max(maxx - minx, 1e-30)
    span_y = max(maxy - miny, 1e-30)
    global_scale = [span_x / (quantize - 1), span_y / (quantize - 1)]
    global_translate = [minx, miny]
    global_kx = 1.0 / global_scale[0]
    global_ky = 1.0 / global_scale[1]

    merged_arcs = []
    merged_geoms = []

    if persistent_tmpdir:
        tmpdir = os.path.abspath(persistent_tmpdir)
        os.makedirs(tmpdir, exist_ok=True)
        cleanup_tmpdir = False
        logging.info(f"Using persistent chunks tmpdir: {tmpdir} (will resume existing chunks)")
    else:
        tmpdir = tempfile.mkdtemp(prefix="topo_chunks_")
        cleanup_tmpdir = True
    try:
        gdf_pickle = os.path.join(tmpdir, "gdf.pkl")
        # Always write a fresh pickle — the preprocessed GDF may differ across
        # runs (simplify tolerance, etc.), and chunk worker slice indices depend
        # on len(gdf).
        with open(gdf_pickle, "wb") as f:
            pickle.dump(gdf_out, f, protocol=pickle.HIGHEST_PROTOCOL)
        logging.info(
            f"Pickled preprocessed GDF to temp "
            f"({os.path.getsize(gdf_pickle)/1e6:.1f} MB) for {chunks} workers"
        )

        for i in range(chunks):
            out_path = os.path.join(tmpdir, f"chunk_{i}_of_{chunks}.topojson")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                logging.info(
                    f"  Chunk {i+1}/{chunks}: reusing existing output "
                    f"({os.path.getsize(out_path)/1e6:.1f} MB)"
                )
            else:
                logging.info(f"  Chunk {i+1}/{chunks}: spawning worker subprocess ...")
                result = subprocess.run(
                    [
                        sys.executable,
                        os.path.abspath(__file__),
                        "--_worker",
                        gdf_pickle,
                        str(i),
                        str(chunks),
                        str(quantize),
                        "1" if topology_enabled else "0",
                        out_path,
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0 or not os.path.exists(out_path):
                    raise RuntimeError(
                        f"Chunk {i+1} worker failed (exit {result.returncode}).\n"
                        f"stdout tail: {result.stdout[-500:]}\n"
                        f"stderr tail: {result.stderr[-1500:]}"
                    )

            with open(out_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not persistent_tmpdir:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

            chunk_scale = d["transform"]["scale"]
            chunk_translate = d["transform"]["translate"]

            arc_offset = len(merged_arcs)
            for arc in d["arcs"]:
                merged_arcs.append(_reencode_arc(
                    arc, chunk_scale, chunk_translate,
                    global_kx, global_ky, global_translate[0], global_translate[1],
                ))

            obj_keys = list(d["objects"].keys())
            obj = d["objects"][obj_keys[0]]
            chunk_geom_count = len(obj.get("geometries", []))
            for g in obj.get("geometries", []):
                _remap_geom_arcs_recursive(g, arc_offset)
                merged_geoms.append(g)

            logging.info(
                f"  Chunk {i+1}/{chunks}: +{len(d['arcs']):,} arcs, "
                f"+{chunk_geom_count:,} geometries "
                f"(running total: {len(merged_arcs):,} arcs, {len(merged_geoms):,} geoms)"
            )
            del d, obj
            gc.collect()

        # Re-sequence the `index` property and `id` so the merged output has
        # 0..N-1 indices across all chunks.
        for new_idx, g in enumerate(merged_geoms):
            if g.get("properties") is not None and "index" in g["properties"]:
                g["properties"]["index"] = new_idx
            if "id" in g:
                g["id"] = str(new_idx)

        return {
            "type": "Topology",
            "objects": {"data": {"type": "GeometryCollection", "geometries": merged_geoms}},
            "arcs": merged_arcs,
            "bbox": [minx, miny, maxx, maxy],
            "transform": {"scale": global_scale, "translate": global_translate},
        }
    finally:
        if cleanup_tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            logging.info(f"Chunks tmpdir preserved at {tmpdir} (delete manually when done).")


# ── main conversion ───────────────────────────────────────────────────────────

def convert(gdb_path, layer, indicator, output_path, quantize, dissolve_by=None, simplify=None, no_topology=False, chunks=1, chunks_tmpdir=None):
    steps = [
        "Reading GDB layer",
        "Reprojecting to EPSG:4326",
        "Filtering columns",
        "Cleaning geometries",
        "Dissolving" if dissolve_by else "Skipping dissolve",
        "Building TopoJSON",
        "Writing output",
    ]
    progress = tqdm(total=len(steps), desc="Overall progress", unit="step", ncols=80)

    def advance(msg):
        progress.set_postfix_str(msg)
        progress.update(1)

    # Step 1 — read only the needed columns (avoids loading 200+ GDB columns)
    logging.info(f"Reading layer '{layer}' from {gdb_path}")
    with fiona.open(gdb_path, layer=layer) as src:
        all_schema_cols = list(src.schema["properties"].keys())
        src_crs = src.crs
    indicator_cols = get_indicator_columns(all_schema_cols, indicator)
    shape_cols = [c for c in ["Shape_Length", "Shape_Area"] if c in all_schema_cols]
    keep_cols = indicator_cols + shape_cols
    gdf = gpd.read_file(gdb_path, layer=layer, columns=keep_cols)
    logging.info(f"Loaded {len(gdf):,} features, CRS: {gdf.crs}")
    advance(f"Loaded {len(gdf):,} features")

    # Step 2 — reproject to EPSG:4326
    if gdf.crs is None:
        logging.warning("CRS is not set; assuming EPSG:4326")
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        logging.info(f"Reprojecting from {gdf.crs} to EPSG:4326")
        gdf = gdf.to_crs("EPSG:4326")
    advance("Reprojected to EPSG:4326")

    # Step 3 — columns already filtered during read
    logging.info(f"Indicator columns found: {indicator_cols}")
    gdf_out = gdf[keep_cols + ["geometry"]].copy()
    advance(f"Kept {len(keep_cols)} indicator columns")

    # Step 4 — clean geometries (buffer(0) only applied to invalid geometries)
    before = len(gdf_out)
    gdf_out = gdf_out[~gdf_out.geometry.is_empty & gdf_out.geometry.notna()]
    invalid_mask = ~gdf_out.geometry.is_valid
    if invalid_mask.any():
        logging.info(f"Fixing {invalid_mask.sum():,} invalid geometries with buffer(0) ...")
        gdf_out.loc[invalid_mask, "geometry"] = gdf_out.loc[invalid_mask, "geometry"].buffer(0)
    gdf_out = gdf_out[gdf_out.geometry.is_valid]
    logging.info(f"Features after geometry cleanup: {len(gdf_out):,} (removed {before - len(gdf_out):,})")
    advance(f"Cleaned geometries ({len(gdf_out):,} valid)")

    # Step 5 — optional dissolve
    if dissolve_by:
        if dissolve_by not in gdf_out.columns:
            progress.close()
            raise ValueError(
                f"--dissolve-by column '{dissolve_by}' not found. "
                f"Available columns: {list(gdf_out.columns)}"
            )
        numeric_cols = [
            c for c in gdf_out.columns
            if c not in (dissolve_by, "geometry")
            and gdf_out[c].dtype.kind in "iufcb"
        ]
        aggfunc = {c: "mean" for c in numeric_cols}
        logging.info(f"Dissolving by '{dissolve_by}' (mean aggregation for numeric columns) ...")
        gdf_out = gdf_out.dissolve(by=dissolve_by, aggfunc=aggfunc, as_index=False).reset_index(drop=True)
        gdf_out = gdf_out[~gdf_out.geometry.is_empty & gdf_out.geometry.notna()]
        gdf_out = gdf_out[gdf_out.geometry.is_valid]
        logging.info(f"Features after dissolve: {len(gdf_out):,}")
        advance(f"Dissolved -> {len(gdf_out):,} features")
    else:
        advance("No dissolve")

    # Add sequential 0-based index
    gdf_out = gdf_out.reset_index(drop=True)
    gdf_out.insert(0, "index", gdf_out.index.astype(int))
    logging.info(f"Output columns: {['index'] + keep_cols}")

    # Optional geometry simplification (strongly recommended after dissolve)
    if simplify:
        logging.info(f"Simplifying geometries (tolerance={simplify}) ...")
        gdf_out["geometry"] = gdf_out["geometry"].simplify(simplify, preserve_topology=True)
        gdf_out = gdf_out[~gdf_out.geometry.is_empty & gdf_out.geometry.notna()]
        logging.info(f"Features after simplification: {len(gdf_out):,}")
        advance(f"Simplified (tolerance={simplify})")
    else:
        if len(gdf_out) > 10000:
            logging.warning(
                f"Processing {len(gdf_out):,} features without simplification may be slow. "
                "Consider --simplify 500 or --no-topology to speed things up."
            )

    # Step 6 — build TopoJSON
    # Auto-disable full topology when dissolve was used and no simplify provided
    # (and no chunking requested) to prevent OOM on large dissolved geometries.
    if dissolve_by and not simplify and not no_topology and chunks <= 1:
        logging.warning(
            "Dissolved geometries without --simplify can exhaust RAM during topology "
            "computation. Automatically switching to --no-topology mode. "
            "Use --simplify 500 or --chunks 4 for a smaller output file."
        )
        no_topology = True

    if chunks > 1:
        logging.info(
            f"Building TopoJSON in {chunks} chunks "
            f"(topology={'False' if no_topology else 'True'}, quantize={quantize}) ..."
        )
        topo_dict = build_topojson_chunked(
            gdf_out, quantize, chunks,
            topology_enabled=not no_topology,
            persistent_tmpdir=chunks_tmpdir,
        )
    elif no_topology:
        logging.info("Building TopoJSON (topology=False, fast mode) ...")
        topo = topojson.Topology(gdf_out, prequantize=quantize, topology=False)
        topo_dict = json.loads(topo.to_json())
    else:
        logging.info(f"Building TopoJSON (quantize={quantize}) ...")
        topo = topojson.Topology(gdf_out, prequantize=quantize)
        topo_dict = json.loads(topo.to_json())
    if "objects" in topo_dict:
        existing_keys = list(topo_dict["objects"].keys())
        if len(existing_keys) == 1 and existing_keys[0] != "data":
            topo_dict["objects"]["data"] = topo_dict["objects"].pop(existing_keys[0])
            logging.info(f"Renamed object key '{existing_keys[0]}' -> 'data'")
    advance("TopoJSON built")

    # Step 7 — write output
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(topo_dict, f, separators=(",", ":"))
    progress.close()

    size_mb = os.path.getsize(output_path) / 1_048_576
    logging.info(f"Written: {output_path}  ({size_mb:.2f} MB)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert an Aqueduct GDB layer/indicator to TopoJSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--gdb", required=True, help="Path to the .gdb file")
    parser.add_argument(
        "--layer",
        default="baseline_annual",
        help="GDB layer name (default: baseline_annual)",
    )
    parser.add_argument(
        "--indicator",
        default=None,
        help=(
            "Indicator column prefix to extract (e.g. bws, bwd, bws_01, bau30_ba). "
            "Required unless --list is specified."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output .topojson file path (default: {indicator}_{layer}.topojson)",
    )
    parser.add_argument(
        "--quantize",
        type=int,
        default=100000,
        help="Prequantization level passed to topojson.Topology (default: 100000)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List layers and indicators in the GDB and exit",
    )
    parser.add_argument(
        "--dissolve-by",
        default=None,
        dest="dissolve_by",
        help="Column name to dissolve/merge geometries by (e.g. bws_label)",
    )
    parser.add_argument(
        "--simplify",
        type=float,
        default=None,
        help="Geometry simplification tolerance in CRS units (e.g. 500 for ~500m in EPSG:3857)",
    )
    parser.add_argument(
        "--no-topology",
        action="store_true",
        dest="no_topology",
        help="Skip arc-sharing topology computation (much faster, slightly larger file)",
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=1,
        help=(
            "Split the input into N chunks and build topology per chunk, then merge. "
            "Use on memory-constrained machines when a single-pass topology OOMs. "
            "Output is slightly larger than a single-pass topology (arc sharing only "
            "within each chunk). Default: 1 (no chunking). Typical values: 4, 8."
        ),
    )
    parser.add_argument(
        "--chunks-tmpdir",
        default=None,
        dest="chunks_tmpdir",
        help=(
            "Persistent directory for per-chunk outputs. If set, completed chunks "
            "are reused on re-run — letting you resume after a failed chunk without "
            "redoing the ones that succeeded. Default: None (auto-cleaned temp dir)."
        ),
    )
    parser.add_argument("--log", default=None, help="Path to log file (optional)")
    return parser.parse_args()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--_worker":
        _worker_build_chunk(sys.argv[2:])
        return

    args = parse_args()
    setup_logging(args.log)

    if not os.path.exists(args.gdb):
        logging.error(f"GDB path does not exist: {args.gdb}")
        sys.exit(1)

    if args.list:
        list_layers_and_indicators(args.gdb)
        sys.exit(0)

    if not args.indicator:
        logging.error("--indicator is required (unless --list is used).")
        sys.exit(1)

    # Validate layer
    available_layers = fiona.listlayers(args.gdb)
    if args.layer not in available_layers:
        logging.error(
            f"Layer '{args.layer}' not found in GDB. "
            f"Available layers: {available_layers}"
        )
        sys.exit(1)

    output_path = build_output_path(args.output, args.indicator, args.layer)

    try:
        convert(
            gdb_path=args.gdb,
            layer=args.layer,
            indicator=args.indicator,
            output_path=output_path,
            quantize=args.quantize,
            dissolve_by=args.dissolve_by,
            simplify=args.simplify,
            no_topology=args.no_topology,
            chunks=args.chunks,
            chunks_tmpdir=args.chunks_tmpdir,
        )
    except ValueError as e:
        logging.error(str(e))
        sys.exit(2)
    except Exception as e:
        logging.exception(f"Unexpected error: {e}")
        sys.exit(99)


if __name__ == "__main__":
    main()
