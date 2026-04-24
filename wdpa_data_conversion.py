import warnings
warnings.filterwarnings("ignore")

import argparse
import geopandas as gpd
import pandas as pd
import topojson
import fiona
import sys
import os
import hashlib
import pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
from shapely.geometry import shape
from shapely.ops import unary_union
from tqdm import tqdm


# Directory where the scan-phase cache is written (next to this script).
SCAN_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scan_cache")


# Number of worker processes for parallel dissolve. None = use all available cores.
WORKERS = None


RAMSAR_FILTER = {
    "RAMSAR",
    "RAMSAR Wetland",
    "RAMSAR Sites",
    "Ramsar Site, Wetland of International Importance",
    "Wetland of International Importance (Ramsar Site)",  # canonical in WDPA Dec 2025+
}
WHS_FILTER = {"World Heritage Site (natural or mixed)"}
IUCN_FILTER = {"Ia", "Ib", "II", "III"}

# Simplification tolerance in degrees (EPSG:4326). ~0.0005 ≈ 55 m at equator.
SIMPLIFY_TOL = 0.0005

# Features per progressive-union chunk — caps peak RAM during dissolve.
DISSOLVE_CHUNK = 500


def find_poly_layer(gdb_path):
    for l in fiona.listlayers(gdb_path):
        if "poly" in l.lower():
            return l
    return None


def _gdb_mtime(gdb_path):
    """Return the max mtime of any file inside the .gdb directory — catches
    any modification to the dataset even when the outer dir mtime doesn't
    update."""
    latest = os.path.getmtime(gdb_path)
    try:
        for name in os.listdir(gdb_path):
            p = os.path.join(gdb_path, name)
            if os.path.isfile(p):
                latest = max(latest, os.path.getmtime(p))
    except OSError:
        pass
    return latest


def scan_cache_path(gdb_path, layer):
    """Build a cache path keyed on inputs that actually affect the scan result.
    Anything that changes downstream output should go into the hash — otherwise
    you'd reuse a stale cache after e.g. bumping SIMPLIFY_TOL."""
    h = hashlib.md5()
    h.update(os.path.abspath(gdb_path).encode())
    h.update(str(_gdb_mtime(gdb_path)).encode())
    h.update(str(layer).encode())
    h.update(str(SIMPLIFY_TOL).encode())
    h.update(repr(sorted(IUCN_FILTER)).encode())
    h.update(repr(sorted(RAMSAR_FILTER)).encode())
    h.update(repr(sorted(WHS_FILTER)).encode())
    key = h.hexdigest()[:16]
    return os.path.join(SCAN_CACHE_DIR, f"scan_{key}.pkl")


def load_scan_cache(cache_path):
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    except (pickle.UnpicklingError, EOFError, OSError):
        # Corrupt/truncated cache — ignore and redo the scan.
        return None


def save_scan_cache(cache_path, payload):
    os.makedirs(SCAN_CACHE_DIR, exist_ok=True)
    # Write to a temp file then rename so a Ctrl+C midway doesn't leave a
    # corrupt pickle behind.
    tmp = cache_path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, cache_path)


def _parallel_pairwise_merge(partials, workers, desc):
    """Tree-reduce a list of geometries to one by pairwise-unioning in parallel
    rounds. Round N halves the count; log2(N) rounds to finish.

    This replaces the single serial unary_union(partials) call that would
    otherwise dominate wall time — that final call is single-threaded inside
    GEOS and can chew on tens of millions of vertices with no progress output,
    which is what looks like a 'hang at 100%' to the user."""
    round_num = 1
    while len(partials) > 1:
        pairs = [partials[i:i + 2] for i in range(0, len(partials), 2)]
        new_partials = [None] * len(pairs)
        n_workers = min(workers, len(pairs))
        with ThreadPoolExecutor(max_workers=n_workers) as ex, \
             tqdm(total=len(pairs), desc=f"{desc} merge r{round_num}",
                  unit="pair", ncols=80) as bar:
            fut_to_idx = {ex.submit(unary_union, p): i for i, p in enumerate(pairs)}
            for fut in as_completed(fut_to_idx):
                new_partials[fut_to_idx[fut]] = fut.result()
                bar.update(1)
        partials = new_partials
        round_num += 1
    return partials[0]


def progressive_union(geoms, chunk=DISSOLVE_CHUNK, desc="Dissolving", workers=WORKERS):
    """Fully-parallel dissolve.

    Phase 1 (initial batching): parallel unary_union of fixed-size chunks.
      Uses as_completed so one slow batch doesn't block the progress bar —
      each batch reports as soon as its thread finishes.

    Phase 2 (pairwise tree reduction): parallel pairwise merges of the
      partials until one remains. Replaces the single serial unary_union over
      all partials — that call is single-threaded in GEOS and was the silent
      hang after 'Dissolving IUCN: 100%'."""
    if not geoms:
        return None
    if len(geoms) <= chunk:
        return unary_union(geoms)

    batches = [geoms[i:i + chunk] for i in range(0, len(geoms), chunk)]
    n_workers = workers or min(os.cpu_count() or 4, len(batches))

    # Phase 1 — parallel initial batches
    partials = []
    with ThreadPoolExecutor(max_workers=n_workers) as ex, \
         tqdm(total=len(geoms), desc=desc, unit="geom", ncols=80) as bar:
        fut_to_size = {ex.submit(unary_union, b): len(b) for b in batches}
        for fut in as_completed(fut_to_size):
            partials.append(fut.result())
            bar.update(fut_to_size[fut])

    # Phase 2 — parallel tree reduction of partials
    return _parallel_pairwise_merge(partials, n_workers, desc)


def write_topojson(gdf, output_path):
    topo = topojson.Topology(gdf).to_json()
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(topo)


def parse_args():
    p = argparse.ArgumentParser(
        description="Convert a WDPA .gdb into iucn.topojson, ramsar.topojson, whs.topojson.",
    )
    p.add_argument("gdb_path", help="Path to the WDPA .gdb directory")
    p.add_argument("output_dir", help="Directory to write topojson outputs")
    p.add_argument("--iucn", action="store_true", help="Build iucn.topojson")
    p.add_argument("--ramsar", action="store_true", help="Build ramsar.topojson")
    p.add_argument("--whs", action="store_true", help="Build whs.topojson")
    args = p.parse_args()
    # No subset flag => build all three (backwards compatible)
    if not (args.iucn or args.ramsar or args.whs):
        args.iucn = args.ramsar = args.whs = True
    return args


def main():
    args = parse_args()
    gdb_path = args.gdb_path
    out_dir = args.output_dir

    if not os.path.exists(gdb_path):
        print(f"GDB path does not exist: {gdb_path}", file=sys.stderr)
        sys.exit(2)
    os.makedirs(out_dir, exist_ok=True)

    layer = find_poly_layer(gdb_path)
    if not layer:
        print("No 'poly' layer found in the GDB.", file=sys.stderr)
        sys.exit(3)

    # ── Scan phase (cached) ──────────────────────────────────────────────
    # The GDB scan + per-feature simplify is the slowest deterministic step
    # (~7 min for 300k features). Cache the trimmed output keyed on the GDB's
    # mtime + simplify tolerance + filters. If any input changes, the cache
    # key changes and we re-scan automatically.
    cache_path = scan_cache_path(gdb_path, layer)
    cached = load_scan_cache(cache_path)

    if cached is not None:
        iucn_geoms = cached["iucn_geoms"]
        ramsar_rows = cached["ramsar_rows"]
        whs_geoms = cached["whs_geoms"]
        src_crs = cached["src_crs"]
        need_reproject = cached["need_reproject"]
        print(f"Loaded scan cache: {cache_path}")
    else:
        iucn_geoms = []
        ramsar_rows = []   # [(desig_eng, geom), ...]
        whs_geoms = []

        with fiona.open(gdb_path, layer=layer) as src:
            total = len(src)
            src_crs = src.crs_wkt
            need_reproject = False
            try:
                epsg = src.crs.get("init", "").lower()
                if "4326" not in epsg and "4326" not in (src_crs or ""):
                    need_reproject = True
            except Exception:
                need_reproject = False

            for feat in tqdm(src, total=total, desc="Scanning GDB", unit="feat", ncols=80):
                props = feat["properties"]
                iucn = props.get("iucn_cat")
                desig = props.get("desig_eng")

                if iucn not in IUCN_FILTER and desig not in RAMSAR_FILTER and desig not in WHS_FILTER:
                    continue

                geom_json = feat["geometry"]
                if geom_json is None:
                    continue
                try:
                    geom = shape(geom_json)
                    if geom.is_empty:
                        continue
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                    if SIMPLIFY_TOL > 0:
                        geom = geom.simplify(SIMPLIFY_TOL, preserve_topology=True)
                    if geom.is_empty:
                        continue
                except Exception:
                    continue

                if iucn in IUCN_FILTER:
                    iucn_geoms.append(geom)
                if desig in RAMSAR_FILTER:
                    ramsar_rows.append((desig, geom))
                if desig in WHS_FILTER:
                    whs_geoms.append(geom)

        save_scan_cache(cache_path, {
            "iucn_geoms": iucn_geoms,
            "ramsar_rows": ramsar_rows,
            "whs_geoms": whs_geoms,
            "src_crs": src_crs,
            "need_reproject": need_reproject,
        })
        print(f"Saved scan cache: {cache_path}  ({os.path.getsize(cache_path)/1_048_576:.1f} MB)")

    print(f"Collected: IUCN={len(iucn_geoms):,}  RAMSAR={len(ramsar_rows):,}  WHS={len(whs_geoms):,}")

    if need_reproject:
        # Reproject the collected subsets only (tiny compared to source).
        def _reproj(geoms):
            s = gpd.GeoSeries(geoms, crs=src_crs).to_crs("EPSG:4326")
            return list(s.values)
        iucn_geoms = _reproj(iucn_geoms)
        whs_geoms = _reproj(whs_geoms)
        if ramsar_rows:
            labels, geoms = zip(*ramsar_rows)
            ramsar_rows = list(zip(labels, _reproj(list(geoms))))

    written = []

    # ── IUCN ─────────────────────────────────────────────────────────────
    if args.iucn and iucn_geoms:
        merged = progressive_union(iucn_geoms, desc="Dissolving IUCN")
        iucn_gdf = gpd.GeoDataFrame(
            {"index": [0], "IUCN_CAT": ["I-III"]},
            geometry=[merged],
            crs="EPSG:4326",
        )
        out_path = os.path.join(out_dir, "iucn.topojson")
        write_topojson(iucn_gdf, out_path)
        written.append(out_path)
        del merged, iucn_gdf
    del iucn_geoms

    # ── RAMSAR ───────────────────────────────────────────────────────────
    if args.ramsar and ramsar_rows:
        by_label = {}
        for lbl, g in ramsar_rows:
            by_label.setdefault(lbl, []).append(g)
        labels, merged_geoms = [], []
        for lbl, gs in by_label.items():
            labels.append(lbl)
            merged_geoms.append(progressive_union(gs, desc=f"Dissolving RAMSAR:{lbl[:20]}"))
        ramsar_gdf = gpd.GeoDataFrame(
            {"DESIG_ENG": labels}, geometry=merged_geoms, crs="EPSG:4326"
        )
        out_path = os.path.join(out_dir, "ramsar.topojson")
        write_topojson(ramsar_gdf, out_path)
        written.append(out_path)
        del by_label, merged_geoms, ramsar_gdf
    del ramsar_rows

    # ── WHS ──────────────────────────────────────────────────────────────
    if args.whs and whs_geoms:
        merged = progressive_union(whs_geoms, desc="Dissolving WHS")
        whs_gdf = gpd.GeoDataFrame(
            {"DESIG_ENG": list(WHS_FILTER)}, geometry=[merged], crs="EPSG:4326"
        )
        out_path = os.path.join(out_dir, "whs.topojson")
        write_topojson(whs_gdf, out_path)
        written.append(out_path)
        del merged, whs_gdf
    del whs_geoms

    for p in written:
        print(f"Wrote {p}  ({os.path.getsize(p)/1_048_576:.2f} MB)")


if __name__ == "__main__":
    main()
