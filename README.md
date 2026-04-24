# NatureRisk Conversion Console

NiceGUI app that triggers the existing `model-tune/` conversion scripts and
persists their outputs into the existing `nature_risk` Postgres schema.

## Setup

```bash
cd "D:/SDM GENAI/NatureRisk/conversion-ui"
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` → `.env` and adjust. Defaults mirror `backend/.env`.

- `DB_*` — your Postgres connection.
- `MODEL_TUNE_DIR` — absolute path to `model-tune/`; blank = sibling folder.
- `WORK_DIR` — staging folder for inputs + outputs (default `./_work`).

## Run

```bash
python app.py
```

Visit <http://localhost:8501>.

## Tabs (single-page app, top nav)

- **Home** — connection status, config, live row counts.
- **Run** — conversion dropdown → input (upload or server path) → options → run with live log → preview → save.
- **Browse** — sub-tabs for TopoJSON Layers / Client TopoJSON / Raster Tiles / Hierarchy.
- **System** — detailed connection + config + row-count view.

## Supported conversions

| UI label | Wraps | Writes to |
|---|---|---|
| KBA → TopoJSON | `GDB_TO_TOPO/KBA/kba_data_conversion.py` | `topojson_layers` (key=`kba`) |
| WDPA → IUCN / RAMSAR / WHS | `GDB_TO_TOPO/WDPA/wdpa_data_conversion.py` | `topojson_layers` (3 keys) |
| Aqueduct Water Baseline → TopoJSON | `GDB_TO_TOPO/AqueductWaterBassline/aqueduct_gdb_topojson.py` | `topojson_layers` (key=`bws_annual`) |
| SCB Assets CSV → TopoJSON | `EXCEL_TO_TOPO/SCB_DATA/scb_assets_distance_topojson.py` | `client_topojson` (type=`sc_assets`) |
| M&M Client CSVs → TopoJSON(s) | `EXCEL_TO_TOPO/CLIENT_DATA/mm_owner_topojson.py` | `client_topojson` (type=`client_assets`) |
| GFC TIFFs → PNG tiles | `TIFF_TO_PNG/GFC/tif_to_png_local.py` | `raster_tiles` + `raster_manifests` |
| GLOBIO Land Use → PNG tiles | `TIFF_TO_PNG/GLOBIO/globio_lu_to_png.py` | same |
| GLOBIO MSA → PNG tiles | `TIFF_TO_PNG/GLOBIO/globio_msa_to_png.py` | same |

## Input modes

- **Upload** — drop a single file or multiple files (`.zip` auto-extracts for GDB-style folder inputs). Stored under `WORK_DIR/inputs/`.
- **Server path** — enter an absolute path already on the machine. Skip upload for large inputs (WDPA, GFC tiles, etc.).

## M&M client mode

The M&M converter has a dropdown:

- **per group (Option A)** — one topojson per owner; UI filters by `le_name` client-side.
- **per client (Option B)** — one topojson per LEI, nested under the owner folder.

Both share the same pass-1 split cache in `model-tune/EXCEL_TO_TOPO/CLIENT_DATA/_owner_split_cache/`.

## Idempotency

All writes upsert on unique keys:

- `topojson_layers.layer_key`
- `client_topojson (client_id, layer_type)`
- `raster_tiles.tile_path`
- `raster_manifests.manifest_path`

Re-running a conversion **replaces** the previous row with the new data. It does not create duplicates.

## What this app does **not** do

- No schema changes. Existing tables are read/written as-is.
- No mutation of production data except via upsert on the keys above.
- No new converters are written here — all logic stays in `model-tune/`.
- No auth. Assumed to be run locally by a developer.
