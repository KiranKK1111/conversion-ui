"""Registry of available conversions.

Each entry defines:
  - key:             stable UI value
  - label:           human display name
  - input_kind:      'file' (single file), 'folder' (directory of files)
  - script_rel:      path to the CLI script under MODEL_TUNE_DIR
  - args_template:   function (inputs, outdir) -> list[str] of CLI args
  - output_spec:     function (outdir) -> list of produced file paths / tile dir spec
  - destination:     one of 'topojson_layers', 'client_topojson', 'raster_tiles'
  - defaults:        fixed metadata (layer_key, layer_group, layer_type, etc.)

Each page in the UI asks for whatever metadata the destination requires
(layer_key for topojson_layers, sector/group/client for client_topojson, etc.)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Conversion:
    key: str
    label: str
    input_kind: str                     # 'file' | 'folder'
    script_rel: str                     # relative to MODEL_TUNE_DIR
    destination: str                    # 'topojson_layers' | 'client_topojson' | 'raster_tiles'
    description: str = ""
    defaults: dict = field(default_factory=dict)
    # Given (input_path: Path, output_dir: Path, extra: dict) -> list[str] of script args
    args_template: Callable[[Path, Path, dict], list[str]] = None
    # Given (output_dir: Path, extra: dict) -> list[Path] of produced topojson files
    output_files: Callable[[Path, dict], list[Path]] = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _kba_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    # python kba_data_conversion.py <gdb> <output_file>
    return [str(Path("GDB_TO_TOPO/KBA/kba_data_conversion.py")),
            str(inp),
            str(outdir / "kba.topojson")]


def _wdpa_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    # python wdpa_data_conversion.py <gdb> <output_dir> [--iucn/--ramsar/--whs]
    args = [str(Path("GDB_TO_TOPO/WDPA/wdpa_data_conversion.py")),
            str(inp), str(outdir)]
    for flag in ("iucn", "ramsar", "whs"):
        if extra.get(f"wdpa_{flag}", True):
            args.append(f"--{flag}")
    return args


def _aqueduct_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    # python aqueduct_gdb_topojson.py --gdb <path> --indicator bws --dissolve-by bws_label --output <file>
    return [
        str(Path("GDB_TO_TOPO/AqueductWaterBassline/aqueduct_gdb_topojson.py")),
        "--gdb", str(inp),
        "--indicator", extra.get("aqueduct_indicator", "bws"),
        "--dissolve-by", "bws_label",
        "--output", str(outdir / "bws_annual.topojson"),
    ]


def _scb_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    # python scb_assets_distance_topojson.py <csv> <output_file>
    return [
        str(Path("EXCEL_TO_TOPO/SCB_DATA/scb_assets_distance_topojson.py")),
        str(inp),
        str(outdir / "sc_assets.topojson"),
    ]


def _mm_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    # python mm_owner_topojson.py <input_dir> <output_dir> [--per-client] [--owner-id X]
    args = [
        str(Path("EXCEL_TO_TOPO/CLIENT_DATA/mm_owner_topojson.py")),
        str(inp), str(outdir),
    ]
    if extra.get("per_client"):
        args.append("--per-client")
    if extra.get("owner_id"):
        args += ["--owner-id", extra["owner_id"]]
    return args


def _gfc_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    return [
        str(Path("TIFF_TO_PNG/GFC/tif_to_png_local.py")),
        "--input-dir", str(inp),
        "--output-dir", str(outdir),
        "--base-url", extra.get("base_url", "http://localhost:3000/geo-png"),
        "--threads", str(extra.get("threads", 4)),
    ]


def _globio_lu_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    return [
        str(Path("TIFF_TO_PNG/GLOBIO/globio_lu_to_png.py")),
        "--input-dir", str(inp),
        "--output-dir", str(outdir),
    ]


def _globio_msa_args(inp: Path, outdir: Path, extra: dict) -> list[str]:
    return [
        str(Path("TIFF_TO_PNG/GLOBIO/globio_msa_to_png.py")),
        "--input-dir", str(inp),
        "--output-dir", str(outdir),
    ]


# Output file discovery — match whatever the script actually wrote
def _topojsons_in(outdir: Path, extra: dict) -> list[Path]:
    return sorted(outdir.rglob("*.topojson"))


def _pngs_in(outdir: Path, extra: dict) -> list[Path]:
    return sorted(outdir.glob("*.png"))


# ── registry ────────────────────────────────────────────────────────────────

CONVERSIONS: list[Conversion] = [
    Conversion(
        key="kba",
        label="KBA → TopoJSON",
        input_kind="folder",
        script_rel="GDB_TO_TOPO/KBA/kba_data_conversion.py",
        destination="topojson_layers",
        description="Key Biodiversity Areas from a .gdb directory.",
        defaults={"layer_key": "kba", "layer_name": "Key Biodiversity Areas (KBA)",
                  "layer_group": "proximity"},
        args_template=_kba_args,
        output_files=_topojsons_in,
    ),
    Conversion(
        key="wdpa",
        label="WDPA → IUCN / RAMSAR / WHS",
        input_kind="folder",
        script_rel="GDB_TO_TOPO/WDPA/wdpa_data_conversion.py",
        destination="topojson_layers",
        description="World Database of Protected Areas → 3 topojson layers.",
        defaults={"layer_group": "proximity"},  # layer_key per-output
        args_template=_wdpa_args,
        output_files=_topojsons_in,
    ),
    Conversion(
        key="aqueduct",
        label="Aqueduct Water Baseline → TopoJSON",
        input_kind="folder",
        script_rel="GDB_TO_TOPO/AqueductWaterBassline/aqueduct_gdb_topojson.py",
        destination="topojson_layers",
        description="WRI Aqueduct baseline water stress (dissolved by bws_label).",
        defaults={"layer_key": "bws_annual", "layer_name": "Baseline Water Stress",
                  "layer_group": "aquaduct"},
        args_template=_aqueduct_args,
        output_files=_topojsons_in,
    ),
    Conversion(
        key="scb",
        label="SCB Assets CSV → TopoJSON",
        input_kind="file",
        script_rel="EXCEL_TO_TOPO/SCB_DATA/scb_assets_distance_topojson.py",
        destination="topojson_layers",
        description="SCB branch/office distance data → sc_assets.topojson.",
        defaults={
            "layer_key": "sc_assets",
            "layer_name": "SCB Asset Locations",
            "layer_group": "client",
        },
        args_template=_scb_args,
        output_files=_topojsons_in,
    ),
    Conversion(
        key="mm_client",
        label="Client Data → TopoJSON(s)",
        input_kind="folder",
        script_rel="EXCEL_TO_TOPO/CLIENT_DATA/mm_owner_topojson.py",
        destination="client_topojson",
        description="Enriched client CSVs (long format) → one per group or per LEI.",
        defaults={"layer_type": "client_assets"},
        args_template=_mm_args,
        output_files=_topojsons_in,
    ),
    Conversion(
        key="gfc",
        label="GFC TIFFs → PNG tiles",
        input_kind="folder",
        script_rel="TIFF_TO_PNG/GFC/tif_to_png_local.py",
        destination="raster_tiles",
        description="Global Forest Change lossyear tiles → PNG with manifest.",
        defaults={"manifest_prefix": "gfc-png"},
        args_template=_gfc_args,
        output_files=_pngs_in,
    ),
    Conversion(
        key="globio_lu",
        label="GLOBIO Land Use → PNG tiles",
        input_kind="folder",
        script_rel="TIFF_TO_PNG/GLOBIO/globio_lu_to_png.py",
        destination="raster_tiles",
        description="GLOBIO land-use categorical raster → PNG tiles.",
        defaults={"manifest_prefix": "globio-lu-png"},
        args_template=_globio_lu_args,
        output_files=_pngs_in,
    ),
    Conversion(
        key="globio_msa",
        label="GLOBIO MSA → PNG tiles",
        input_kind="folder",
        script_rel="TIFF_TO_PNG/GLOBIO/globio_msa_to_png.py",
        destination="raster_tiles",
        description="GLOBIO Mean Species Abundance raster → PNG tiles.",
        defaults={"manifest_prefix": "globio-msa-png"},
        args_template=_globio_msa_args,
        output_files=_pngs_in,
    ),
]


def get_conversion(key: str) -> Conversion:
    for c in CONVERSIONS:
        if c.key == key:
            return c
    raise KeyError(f"Unknown conversion: {key!r}")
