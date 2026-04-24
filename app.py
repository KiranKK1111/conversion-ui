"""
SDM NatureRisk – Conversion Console (NiceGUI)
=============================================
Run:
    python app.py
Visit: http://localhost:8501
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
import zipfile
from pathlib import Path

import pandas as pd
from nicegui import app, ui
from nicegui.events import UploadEventArguments

from config import settings
from converters import CONVERSIONS, get_conversion
from db import (
    check_connection,
    ensure_history_schema,
    get_hierarchy,
    get_topojson_data,
    list_client_topojsons,
    list_history,
    list_raster_manifests,
    list_raster_tiles,
    list_topojson_layers,
    save_client_topojson,
    save_raster_manifest,
    save_raster_tile,
    save_topojson_layer,
    table_row_counts,
    upsert_client,
)


# Ensure the historical_data table exists (idempotent).
try:
    ensure_history_schema()
except Exception as _e:
    # Surface later via System tab if connection is broken; don't crash import.
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Run tab state — one in-flight conversion at a time is fine for local dev.
#  Move to app.storage.tab / app.storage.user if this is ever multi-user.
# ─────────────────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self) -> None:
        self.conv_key: str | None = None
        self.input_path: Path | None = None
        self.output_dir: Path | None = None
        self.extra: dict = {}
        self.produced: list[Path] = []
        self.last_ok: bool = False
        self.running: bool = False
        self.hierarchy: pd.DataFrame | None = None  # scan result for Client Data


state = RunState()


# ─────────────────────────────────────────────────────────────────────────────
#  Small utilities
# ─────────────────────────────────────────────────────────────────────────────

def _size_str(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:,.1f} {unit}"
        x /= 1024
    return f"{x:,.1f} TB"


def _topojson_meta(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}
    obj = next(iter(d.get("objects", {}).values()), {})
    return {
        "file": p.name,
        "size": _size_str(p.stat().st_size),
        "feature_count": len(obj.get("geometries", [])),
        "arcs": len(d.get("arcs", [])),
        "bbox": d.get("bbox"),
    }


def _stage_dir(prefix: str) -> Path:
    d = settings.WORK_DIR / "inputs" / f"{prefix}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _output_dir(conv_key: str) -> Path:
    d = settings.WORK_DIR / "outputs" / f"{conv_key}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─────────────────────────────────────────────────────────────────────────────
#  M&M client CSV — hierarchy scan + pre-filter
#  The enriched CSVs are in long format with columns: sector, owner, le_name, ...
#  We let the user scan the input, then pick from dependent dropdowns. If any
#  filter is applied, we stage a filtered copy of the CSVs in WORK_DIR and
#  point the conversion script at that folder — keeps the script unchanged.
# ─────────────────────────────────────────────────────────────────────────────

_ALL = "(All)"


def _list_csvs(path: Path) -> list[Path]:
    return sorted(path.rglob("*.csv")) if path.is_dir() else ([path] if path.suffix.lower() == ".csv" else [])


def _scan_hierarchy(path: Path) -> pd.DataFrame:
    """Scan CSV(s) under `path` and return a DataFrame with unique
    (sector, owner, le_name) rows. Only those 3 columns are read — even on
    41M-row inputs this runs in a couple of minutes."""
    files = _list_csvs(path)
    if not files:
        raise ValueError(f"No CSV files found at {path}")
    combined: list[pd.DataFrame] = []
    for f in files:
        chunks_unique: list[pd.DataFrame] = []
        for chunk in pd.read_csv(
            f,
            usecols=["sector", "owner", "le_name"],
            encoding="ISO-8859-1",
            chunksize=500_000,
        ):
            chunks_unique.append(chunk.drop_duplicates())
        if chunks_unique:
            combined.append(pd.concat(chunks_unique, ignore_index=True).drop_duplicates())
    if not combined:
        return pd.DataFrame(columns=["sector", "owner", "le_name"])
    return (
        pd.concat(combined, ignore_index=True)
        .drop_duplicates()
        .sort_values(["sector", "owner", "le_name"], na_position="last")
        .reset_index(drop=True)
    )


def _apply_filter_to_csvs(src: Path, filters: dict) -> Path:
    """If any filter is set to something other than (All), write filtered copies
    of the CSVs to a fresh staging folder and return that. Otherwise return
    `src` unchanged. Filters: {"sector": ..., "owner": ..., "le_name": ...}."""
    active = {k: v for k, v in filters.items() if v and v != _ALL}
    if not active:
        return src
    dest = settings.WORK_DIR / "inputs" / f"mm_filtered_{uuid.uuid4().hex[:8]}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in _list_csvs(src):
        pieces: list[pd.DataFrame] = []
        for chunk in pd.read_csv(
            f,
            dtype={"asset_id": str, "id": str},
            encoding="ISO-8859-1",
            chunksize=200_000,
        ):
            mask = pd.Series(True, index=chunk.index)
            for col, val in active.items():
                mask &= chunk[col].astype(str) == str(val)
            pieces.append(chunk[mask])
        df = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
        if len(df) > 0:
            df.to_csv(dest / f.name, index=False, encoding="ISO-8859-1")
    return dest


# ─────────────────────────────────────────────────────────────────────────────
#  HOME tab
# ─────────────────────────────────────────────────────────────────────────────

def render_home(on_get_started) -> None:
    """Welcome splash with a single CTA into the Run tab."""
    with ui.column().classes(
        "w-full items-center justify-center gap-5 q-pa-xl"
    ).style("min-height: 70vh"):
        ui.icon("eco", size="5rem").classes("text-primary")
        ui.label("NatureRisk Conversion Console").classes(
            "text-h3 text-weight-bold text-center"
        )
        ui.label(
            "Turn geospatial source data — GDB archives, CSV datasets and "
            "TIFF rasters — into web-ready TopoJSON layers and PNG tile "
            "sets, and persist them directly into your Postgres schema. "
            "Each run tracks changes, so repeat conversions only re-persist "
            "what has actually changed."
        ).classes("text-subtitle1 text-center text-grey-5").style(
            "max-width: 720px"
        )
        ui.button("Get started →", on_click=on_get_started).props(
            "size=lg color=primary unelevated no-caps"
        ).classes("q-mt-md")


# ─────────────────────────────────────────────────────────────────────────────
#  RUN CONVERSION tab
# ─────────────────────────────────────────────────────────────────────────────

def render_run() -> None:
    with ui.column().classes("w-full gap-4 p-4"):
        ui.label("Run conversion").classes("text-h5 text-weight-bold")

        # Step 1 — pick conversion
        with ui.card().classes("w-full"):
            ui.label("1 · Conversion type").classes("text-subtitle1 text-weight-bold")
            conv_options = {c.key: c.label for c in CONVERSIONS}
            selected = ui.select(
                options=conv_options,
                label="Select a converter",
                value=CONVERSIONS[0].key,
                on_change=lambda e: _on_conv_change(e.value),
            ).classes("w-full")
            state.conv_key = selected.value
            conv_desc = ui.label(get_conversion(selected.value).description).classes(
                "text-caption text-grey-7"
            )

        # Containers that re-render when the conversion changes
        input_card = ui.card().classes("w-full")
        options_card = ui.card().classes("w-full")
        dest_card = ui.card().classes("w-full")
        run_card = ui.card().classes("w-full")

        # Step 2 — input
        log_widget: ui.log = None  # set inside _build_run_card

        def _on_conv_change(key: str) -> None:
            state.conv_key = key
            state.input_path = None
            state.produced = []
            state.last_ok = False
            state.hierarchy = None
            conv_desc.text = get_conversion(key).description
            _build_input_card()
            _build_options_card()
            _build_dest_card()
            _build_run_card()
            _build_preview_card()

        # ── 2 · INPUT ───────────────────────────────────────────────────────
        def _build_input_card() -> None:
            input_card.clear()
            conv = get_conversion(state.conv_key)
            with input_card:
                ui.label("2 · Input").classes("text-subtitle1 text-weight-bold")

                mode = {"value": "Server path"}  # sane default for big GDBs/folders
                mode_toggle = ui.toggle(
                    ["Upload", "Server path"],
                    value="Server path",
                ).props("color=primary")
                ui.label(
                    "Upload: drop file(s) or .zip (auto-extracted). "
                    "Server path: absolute path to a file or folder already on this machine."
                ).classes("text-caption text-grey-7")

                upload_box = ui.column().classes("w-full")
                path_box = ui.column().classes("w-full")

                def _render_upload() -> None:
                    upload_box.clear()
                    path_box.visible = False
                    upload_box.visible = True
                    stage = _stage_dir(conv.key)

                    def on_upload(e: UploadEventArguments) -> None:
                        target = stage / e.name
                        target.write_bytes(e.content.read())
                        if e.name.lower().endswith(".zip"):
                            try:
                                with zipfile.ZipFile(target) as zf:
                                    zf.extractall(stage)
                                target.unlink()
                                ui.notify(f"Extracted {e.name}", type="positive")
                            except Exception as ex:
                                ui.notify(f"Zip extract failed: {ex}", type="negative")
                        state.input_path = stage if conv.input_kind == "folder" else target
                        path_display.set_text(f"Staged at: {state.input_path}")

                    with upload_box:
                        ui.upload(
                            on_upload=on_upload,
                            multiple=(conv.input_kind == "folder"),
                            auto_upload=True,
                            max_file_size=2 * 1024 * 1024 * 1024,  # 2 GB
                        ).props(f'label="Drop {"files/folder/zip" if conv.input_kind=="folder" else "file"}"').classes("w-full")
                        path_display = ui.label("").classes("text-caption text-grey-7")

                def _render_path() -> None:
                    path_box.clear()
                    upload_box.visible = False
                    path_box.visible = True
                    with path_box:
                        inp = ui.input(
                            label=f"Absolute path to {'folder' if conv.input_kind=='folder' else 'file'}",
                            value=str(settings.MODEL_TUNE_DIR),
                        ).classes("w-full")
                        status = ui.label("").classes("text-caption")

                        def _check() -> None:
                            p = Path(inp.value).expanduser()
                            if not p.exists():
                                status.set_text(f"✗ Does not exist: {p}")
                                status.classes("text-negative", remove="text-positive text-grey-7")
                                state.input_path = None
                                return
                            state.input_path = p
                            if p.is_dir():
                                count = sum(1 for _ in p.rglob("*"))
                                status.set_text(f"✓ Folder with {count:,} entries")
                            else:
                                status.set_text(f"✓ File · {_size_str(p.stat().st_size)}")
                            status.classes("text-positive", remove="text-negative text-grey-7")

                        inp.on("blur", _check)
                        _check()

                def _on_mode_change() -> None:
                    if mode_toggle.value == "Upload":
                        _render_upload()
                    else:
                        _render_path()

                mode_toggle.on_value_change(_on_mode_change)
                _render_path()

        # ── 3 · OPTIONS (conversion-specific) ──────────────────────────────
        def _build_options_card() -> None:
            options_card.clear()
            conv = get_conversion(state.conv_key)
            with options_card:
                ui.label("3 · Options").classes("text-subtitle1 text-weight-bold")
                state.extra = {}

                if conv.key == "wdpa":
                    with ui.row().classes("gap-4"):
                        iucn = ui.checkbox("Build iucn.topojson", value=True)
                        ramsar = ui.checkbox("Build ramsar.topojson", value=True)
                        whs = ui.checkbox("Build whs.topojson", value=True)
                    iucn.bind_value(state.extra, "wdpa_iucn")
                    ramsar.bind_value(state.extra, "wdpa_ramsar")
                    whs.bind_value(state.extra, "wdpa_whs")
                    state.extra.update({"wdpa_iucn": True, "wdpa_ramsar": True, "wdpa_whs": True})

                elif conv.key == "mm_client":
                    # Output granularity (independent of filtering)
                    mode = ui.select(
                        {"group": "per group (one file per owner)",
                         "client": "per client (one file per LEI)"},
                        label="Output granularity",
                        value="group",
                    ).classes("w-full")
                    mode.on_value_change(lambda e: state.extra.update({"per_client": e.value == "client"}))
                    state.extra["per_client"] = False

                    # Scan controls + dependent dropdowns for sector/group/LE
                    ui.separator()
                    ui.label("Filter by hierarchy").classes("text-subtitle2 text-weight-bold")
                    ui.label(
                        "Scan the input CSV(s) to populate the dropdowns. "
                        "Leave any dropdown on “(All)” to skip that filter."
                    ).classes("text-caption text-grey-7")

                    with ui.row().classes("items-center gap-3"):
                        scan_btn = ui.button("Scan CSVs", icon="search", color="primary")
                        scan_status = ui.label("Not scanned yet").classes("text-caption text-grey-7")

                    dropdowns_row = ui.row().classes("w-full gap-2")
                    with dropdowns_row:
                        sector_sel = ui.select([_ALL], label="Sector", value=_ALL).classes("flex-1")
                        owner_sel = ui.select([_ALL], label="Group (owner)", value=_ALL).classes("flex-1")
                        le_sel = ui.select([_ALL], label="Client (le_name)", value=_ALL).classes("flex-1")
                        sector_sel.disable(); owner_sel.disable(); le_sel.disable()

                    # Seed extras so the run-time filter logic reads something valid
                    state.extra.update({
                        "filter_sector": _ALL, "filter_owner": _ALL, "filter_le_name": _ALL,
                    })
                    sector_sel.bind_value(state.extra, "filter_sector")
                    owner_sel.bind_value(state.extra, "filter_owner")
                    le_sel.bind_value(state.extra, "filter_le_name")

                    def _apply_hier(hier: pd.DataFrame) -> None:
                        """Wire dependent dropdowns on the scanned hierarchy DataFrame."""
                        state.hierarchy = hier

                        def _sectors() -> list[str]:
                            return [_ALL] + sorted(hier["sector"].dropna().astype(str).unique().tolist())

                        def _owners_for(sector: str) -> list[str]:
                            f = hier if sector == _ALL else hier[hier["sector"].astype(str) == sector]
                            return [_ALL] + sorted(f["owner"].dropna().astype(str).unique().tolist())

                        def _les_for(sector: str, owner: str) -> list[str]:
                            f = hier
                            if sector != _ALL:
                                f = f[f["sector"].astype(str) == sector]
                            if owner != _ALL:
                                f = f[f["owner"].astype(str) == owner]
                            return [_ALL] + sorted(f["le_name"].dropna().astype(str).unique().tolist())

                        sector_sel.options = _sectors()
                        sector_sel.value = _ALL
                        sector_sel.update()
                        owner_sel.options = _owners_for(_ALL)
                        owner_sel.value = _ALL
                        owner_sel.update()
                        le_sel.options = _les_for(_ALL, _ALL)
                        le_sel.value = _ALL
                        le_sel.update()
                        sector_sel.enable(); owner_sel.enable(); le_sel.enable()

                        def _on_sector(_e) -> None:
                            owner_sel.options = _owners_for(sector_sel.value)
                            owner_sel.value = _ALL
                            owner_sel.update()
                            le_sel.options = _les_for(sector_sel.value, _ALL)
                            le_sel.value = _ALL
                            le_sel.update()

                        def _on_owner(_e) -> None:
                            le_sel.options = _les_for(sector_sel.value, owner_sel.value)
                            le_sel.value = _ALL
                            le_sel.update()

                        sector_sel.on_value_change(_on_sector)
                        owner_sel.on_value_change(_on_owner)

                    async def _scan() -> None:
                        if not state.input_path:
                            ui.notify("Provide input first (upload or server path)", type="warning")
                            return
                        scan_btn.disable()
                        scan_status.text = "Scanning…"
                        try:
                            hier = await asyncio.to_thread(_scan_hierarchy, state.input_path)
                            if hier.empty:
                                scan_status.text = "No sector/owner/le_name columns found"
                                ui.notify("CSV doesn't have the expected columns", type="warning")
                                return
                            scan_status.text = (
                                f"{len(hier):,} unique combinations · "
                                f"{hier['sector'].nunique()} sectors · "
                                f"{hier['owner'].nunique()} groups · "
                                f"{hier['le_name'].nunique()} LEIs"
                            )
                            _apply_hier(hier)
                            _build_dest_card()   # reveal destination now that hierarchy is known
                            ui.notify("Scan complete", type="positive")
                        except Exception as e:
                            scan_status.text = f"Scan failed: {e}"
                            ui.notify(f"Scan failed: {e}", type="negative")
                        finally:
                            scan_btn.enable()

                    scan_btn.on_click(_scan)

                elif conv.key == "gfc":
                    base = ui.input("Manifest base URL", value="http://localhost:3000/geo-png").classes("w-full")
                    threads = ui.number("Threads", value=4, min=1, max=32).classes("w-full")
                    base.bind_value(state.extra, "base_url")
                    threads.bind_value(state.extra, "threads")
                    state.extra.update({"base_url": base.value, "threads": int(threads.value)})

                else:
                    ui.label("No options for this converter.").classes("text-caption text-grey-7")

        # ── 4 · DESTINATION METADATA ────────────────────────────────────────
        def _build_dest_card() -> None:
            dest_card.clear()
            conv = get_conversion(state.conv_key)

            # For Client Data, hide destination until the scan has run.
            if conv.key == "mm_client" and state.hierarchy is None:
                dest_card.set_visibility(False)
                return
            dest_card.set_visibility(True)

            with dest_card:
                ui.label("4 · Destination metadata").classes("text-subtitle1 text-weight-bold")

                if conv.destination == "topojson_layers":
                    if conv.key == "wdpa":
                        ui.label(
                            "WDPA writes 3 layers (iucn/ramsar/whs) in group 'proximity'. "
                            "No extra metadata needed."
                        ).classes("text-caption")
                    else:
                        with ui.grid(columns=2).classes("w-full gap-2"):
                            lk = ui.input("layer_key", value=conv.defaults.get("layer_key", ""))
                            ln = ui.input("layer_name", value=conv.defaults.get("layer_name", ""))
                            lg = ui.input("layer_group", value=conv.defaults.get("layer_group", "proximity"))
                            desc = ui.input("description")
                        lk.bind_value(state.extra, "layer_key")
                        ln.bind_value(state.extra, "layer_name")
                        lg.bind_value(state.extra, "layer_group")
                        desc.bind_value(state.extra, "description")
                        state.extra.update({
                            "layer_key": lk.value, "layer_name": ln.value,
                            "layer_group": lg.value, "description": desc.value,
                        })

                elif conv.destination == "client_topojson":
                    # For Client Data, seed defaults from the filter dropdowns
                    # when the user has narrowed to a specific sector/group/LE.
                    def _seed(filter_key: str, fallback: str) -> str:
                        v = state.extra.get(filter_key)
                        return v if v and v != _ALL else fallback

                    sector_default = _seed("filter_sector", "Metals & Minings") if conv.key == "mm_client" else "Metals & Minings"
                    group_default = _seed("filter_owner", "All groups") if conv.key == "mm_client" else "SCB"
                    client_default = _seed("filter_le_name", group_default) if conv.key == "mm_client" else "SCB Assets"

                    with ui.grid(columns=3).classes("w-full gap-2"):
                        s = ui.input("Sector", value=sector_default)
                        g = ui.input("Group", value=group_default)
                        c = ui.input("Client", value=client_default)
                    s.bind_value(state.extra, "sector_name")
                    g.bind_value(state.extra, "group_name")
                    c.bind_value(state.extra, "client_name")
                    state.extra.update({
                        "sector_name": s.value, "group_name": g.value, "client_name": c.value,
                        "layer_type": conv.defaults.get("layer_type", "client_assets"),
                    })

                elif conv.destination == "raster_tiles":
                    pfx = ui.input(
                        "Manifest prefix (used as tile_path prefix)",
                        value=conv.defaults.get("manifest_prefix", conv.key),
                    ).classes("w-full")
                    pfx.bind_value(state.extra, "manifest_prefix")
                    state.extra["manifest_prefix"] = pfx.value

        # ── 5 · RUN ─────────────────────────────────────────────────────────
        def _build_run_card() -> None:
            run_card.clear()
            with run_card:
                ui.label("5 · Run").classes("text-subtitle1 text-weight-bold")
                nonlocal log_widget
                with ui.row().classes("items-center gap-2"):
                    run_btn = ui.button("Run conversion", icon="play_arrow", color="primary")
                    stop_btn = ui.button("Stop", icon="stop", color="negative").props("flat")
                    stop_btn.set_visibility(False)
                    status_label = ui.label("Ready").classes("text-caption text-grey-7")
                log_widget = ui.log(max_lines=500).classes("w-full h-80 bg-grey-10 text-white").style("font-family: monospace;")

                async def _run() -> None:
                    if state.running:
                        ui.notify("Already running", type="warning"); return
                    if not state.input_path:
                        ui.notify("Select or upload an input first", type="warning"); return
                    conv = get_conversion(state.conv_key)
                    state.output_dir = _output_dir(conv.key)
                    state.produced = []
                    state.last_ok = False
                    state.running = True
                    log_widget.clear()
                    status_label.text = "Running…"
                    run_btn.disable()
                    stop_btn.set_visibility(True)
                    try:
                        # For M&M: apply sector/owner/le_name filters by staging
                        # a filtered copy of the input CSVs. Keeps the underlying
                        # script unchanged.
                        effective_input = state.input_path
                        if conv.key == "mm_client":
                            filters = {
                                "sector":  state.extra.get("filter_sector"),
                                "owner":   state.extra.get("filter_owner"),
                                "le_name": state.extra.get("filter_le_name"),
                            }
                            active = {k: v for k, v in filters.items() if v and v != _ALL}
                            if active:
                                log_widget.push(f"[ui] filtering input by {active}")
                                status_label.text = "Filtering CSVs…"
                                effective_input = await asyncio.to_thread(
                                    _apply_filter_to_csvs, state.input_path, filters
                                )
                                log_widget.push(f"[ui] staged filtered input → {effective_input}")
                                status_label.text = "Running…"

                        args = conv.args_template(effective_input, state.output_dir, state.extra)
                        cmdline = " ".join(str(a) for a in args)
                        log_widget.push(f"$ python -u {cmdline}")
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-u", *args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                            cwd=str(settings.MODEL_TUNE_DIR),
                        )
                        state._proc = proc
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            log_widget.push(line.decode(errors="replace").rstrip())
                        code = await proc.wait()
                        state.last_ok = code == 0
                        state.produced = conv.output_files(state.output_dir, state.extra)
                        status_label.text = (
                            f"✓ Finished · {len(state.produced)} file(s) produced"
                            if state.last_ok
                            else f"✗ Failed (exit {code})"
                        )
                        ui.notify(
                            "Conversion finished" if state.last_ok else f"Failed (exit {code})",
                            type="positive" if state.last_ok else "negative",
                        )
                        _build_preview_card()
                    except Exception as e:
                        status_label.text = f"✗ Error: {e}"
                        ui.notify(f"Error: {e}", type="negative")
                    finally:
                        state.running = False
                        run_btn.enable()
                        stop_btn.set_visibility(False)

                def _stop() -> None:
                    proc = getattr(state, "_proc", None)
                    if proc and proc.returncode is None:
                        proc.terminate()
                        ui.notify("Stop signal sent", type="warning")

                run_btn.on_click(_run)
                stop_btn.on_click(_stop)

        # ── 6 · PREVIEW + SAVE ──────────────────────────────────────────────
        preview_card = ui.card().classes("w-full")

        def _build_preview_card() -> None:
            preview_card.clear()
            with preview_card:
                ui.label("6 · Preview & save").classes("text-subtitle1 text-weight-bold")
                if not state.produced:
                    ui.label("Run a conversion first — produced files will appear here.").classes(
                        "text-caption text-grey-7"
                    )
                    return

                for p in state.produced:
                    with ui.expansion(f"📄 {p.name}  ·  {_size_str(p.stat().st_size)}", value=False).classes("w-full"):
                        if p.suffix == ".topojson":
                            ui.json_editor({"content": {"json": _topojson_meta(p)}}).classes("w-full")
                        elif p.suffix == ".png":
                            ui.image(str(p)).classes("max-w-md")
                        else:
                            ui.label(f"(no preview for {p.suffix})")

                async def _save() -> None:
                    conv = get_conversion(state.conv_key)
                    try:
                        result = await asyncio.to_thread(
                            _persist, conv, state.produced, state.output_dir, state.extra
                        )
                        saved, skipped, errors = result["saved"], result["skipped"], result["errors"]

                        if saved == 0 and skipped > 0 and not errors:
                            ui.notify(
                                f"No changes detected — {skipped} file(s) already identical in DB",
                                type="info", multi_line=True, timeout=4000,
                            )
                        else:
                            parts = []
                            if saved:   parts.append(f"{saved} saved/updated")
                            if skipped: parts.append(f"{skipped} skipped (unchanged)")
                            if errors:  parts.append(f"{len(errors)} error(s)")
                            ui.notify(
                                " · ".join(parts) if parts else "Done",
                                type="positive" if not errors else "warning",
                                multi_line=True, timeout=4000,
                            )
                        if errors:
                            for name, msg in errors[:5]:
                                tqdm_err = f"{name}: {msg}"
                                ui.notify(tqdm_err, type="negative", timeout=5000)

                        render_browse_tables.refresh()
                        render_system.refresh()
                    except Exception as e:
                        ui.notify(f"Save failed: {e}", type="negative")

                ui.button("Save to database", icon="save", color="primary", on_click=_save).props("unelevated")

        # Kick off first render
        _build_input_card()
        _build_options_card()
        _build_dest_card()
        _build_run_card()
        _build_preview_card()


def _persist(conv, produced: list[Path], outdir: Path, extra: dict) -> dict:
    """Save each produced file. Returns a status dict:
        {"saved": N, "skipped": M, "errors": [...]}"""
    saved, skipped, errors = 0, 0, []

    def _tally(result: tuple[str, str]) -> None:
        nonlocal saved, skipped
        status, _ = result
        if status == "saved":
            saved += 1
        elif status == "skipped":
            skipped += 1

    if conv.destination == "topojson_layers":
        if conv.key == "wdpa":
            name_to_meta = {
                "iucn.topojson":   ("iucn",   "IUCN Protected Areas (WDPA)", "World Database of Protected Areas"),
                "ramsar.topojson": ("ramsar", "Ramsar Wetlands",             "Ramsar Convention Wetlands"),
                "whs.topojson":    ("whs",    "World Heritage Sites",        "UNESCO World Heritage Sites"),
            }
            for p in produced:
                if p.name in name_to_meta:
                    lk, ln, desc = name_to_meta[p.name]
                    try:
                        _tally(save_topojson_layer(lk, ln, "proximity", p, description=desc))
                    except Exception as e:
                        errors.append((p.name, str(e)))
        else:
            for p in produced:
                try:
                    _tally(save_topojson_layer(
                        extra["layer_key"], extra["layer_name"], extra["layer_group"],
                        p, description=extra.get("description") or None,
                    ))
                except Exception as e:
                    errors.append((p.name, str(e)))

    elif conv.destination == "client_topojson":
        client_id = upsert_client(
            extra["sector_name"], extra["group_name"], extra["client_name"],
        )
        for p in produced:
            try:
                _tally(save_client_topojson(
                    client_id, extra["layer_type"],
                    layer_name=p.stem, topojson_path=p,
                ))
            except Exception as e:
                errors.append((p.name, str(e)))

    elif conv.destination == "raster_tiles":
        prefix = extra.get("manifest_prefix", conv.key)
        manifest_path = outdir / "local_manifest.json"
        manifest_data: list = []
        if manifest_path.exists():
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            _tally(save_raster_manifest(f"{prefix}/local_manifest.json", manifest_data))
        except Exception as e:
            errors.append(("manifest", str(e)))
        for p in produced:
            if p.suffix == ".png":
                try:
                    _tally(save_raster_tile(f"{prefix}/{p.name}", p))
                except Exception as e:
                    errors.append((p.name, str(e)))

    return {"saved": saved, "skipped": skipped, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
#  BROWSE DATABASE tab
# ─────────────────────────────────────────────────────────────────────────────

@ui.refreshable
def render_browse_tables() -> None:
    with ui.column().classes("w-full gap-3 p-4"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Browse database").classes("text-h5 text-weight-bold")
            ui.button(icon="refresh", on_click=render_browse_tables.refresh).props(
                "flat dense color=primary"
            ).tooltip("Refresh")

        with ui.tabs().classes("w-full") as sub_tabs:
            t_topo = ui.tab("TopoJSON Layers", icon="public")
            t_client = ui.tab("Client TopoJSON", icon="business")
            t_raster = ui.tab("Raster Tiles", icon="image")
            t_hier = ui.tab("Hierarchy", icon="account_tree")
            t_hist = ui.tab("History", icon="history")

        with ui.tab_panels(sub_tabs, value=t_topo).classes("w-full"):
            with ui.tab_panel(t_topo):
                rows = list_topojson_layers()
                if rows:
                    _table_from_df(pd.DataFrame(rows))
                else:
                    ui.label("No topojson_layers rows yet.").classes("text-grey-7")

            with ui.tab_panel(t_client):
                rows = list_client_topojsons()
                if rows:
                    _table_from_df(pd.DataFrame(rows))
                else:
                    ui.label("No client_topojson rows yet.").classes("text-grey-7")

            with ui.tab_panel(t_raster):
                with ui.row().classes("w-full gap-4"):
                    with ui.column().classes("flex-1"):
                        ui.label("Manifests").classes("text-subtitle1 text-weight-bold")
                        rows = list_raster_manifests()
                        if rows:
                            _table_from_df(pd.DataFrame(rows))
                        else:
                            ui.label("No manifests yet.").classes("text-grey-7")
                    with ui.column().classes("flex-1"):
                        ui.label("Tiles (first 200)").classes("text-subtitle1 text-weight-bold")
                        rows = list_raster_tiles(200)
                        if rows:
                            _table_from_df(pd.DataFrame(rows))
                        else:
                            ui.label("No tiles yet.").classes("text-grey-7")

            with ui.tab_panel(t_hier):
                rows = get_hierarchy()
                if rows:
                    _table_from_df(pd.DataFrame(rows))
                else:
                    ui.label("No clients registered yet.").classes("text-grey-7")

            with ui.tab_panel(t_hist):
                try:
                    rows = list_history(200)
                except Exception as e:
                    ui.label(f"History unavailable: {e}").classes("text-negative")
                    rows = []
                if rows:
                    _table_from_df(pd.DataFrame(rows))
                else:
                    ui.label(
                        "No archived history yet. Every time a conversion's "
                        "output replaces an existing DB row, the old version "
                        "is recorded here."
                    ).classes("text-grey-7")


def _table_from_df(df: pd.DataFrame, max_height: str = "55vh") -> None:
    """Render a DataFrame as a NiceGUI table with a fixed max-height and virtual
    scrolling — the table scrolls internally while the header stays pinned."""
    cols = [
        {"name": c, "label": c.replace("_", " ").title(), "field": c, "align": "left",
         "sortable": True}
        for c in df.columns
    ]
    rows = df.astype(str).to_dict(orient="records")
    (
        ui.table(columns=cols, rows=rows, row_key=cols[0]["field"])
        .classes("w-full")
        .style(f"max-height: {max_height}")
        .props("virtual-scroll dense")
    )


def _scrolling_table(df: pd.DataFrame, max_height: str = "65vh") -> None:
    """Alias kept for back-compat with the System page."""
    _table_from_df(df, max_height=max_height)


# ─────────────────────────────────────────────────────────────────────────────
#  SYSTEM STATUS tab
# ─────────────────────────────────────────────────────────────────────────────

@ui.refreshable
def render_system() -> None:
    with ui.column().classes("w-full gap-3 p-4"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Row counts").classes("text-h5 text-weight-bold")
            ui.button(icon="refresh", on_click=render_system.refresh).props(
                "flat dense color=primary"
            ).tooltip("Refresh")

        try:
            rows = table_row_counts()
            _scrolling_table(pd.DataFrame(rows, columns=["table", "count"]))
        except Exception as e:
            ui.label(f"Could not read counts: {e}").classes("text-negative")


# ─────────────────────────────────────────────────────────────────────────────
#  Main layout / entry
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/")
def main_page() -> None:
    # Theming
    ui.colors(primary="#10b981", secondary="#6366f1")
    dark = ui.dark_mode(value=True)

    with ui.header(elevated=True).classes("items-center bg-primary q-px-md"):
        with ui.row().classes("items-center gap-3 q-mr-lg"):
            ui.icon("eco", size="lg").classes("text-white")
            ui.label("NatureRisk Conversion Console").classes("text-h6 text-white")
        with ui.tabs().props("inline-label dense").classes("text-white") as tabs:
            t_home = ui.tab("Home", icon="home")
            t_run = ui.tab("Run", icon="play_arrow")
            t_browse = ui.tab("Browse", icon="storage")
            t_system = ui.tab("System", icon="health_and_safety")
        ui.space()
        ui.button(icon="brightness_6", on_click=dark.toggle).props(
            "flat round color=white"
        ).tooltip("Toggle dark mode")

    panels = ui.tab_panels(tabs, value=t_home).classes("w-full")
    with panels:
        with ui.tab_panel(t_home):
            # Clicking "Get started" on Home jumps to the Run tab
            render_home(on_get_started=lambda: panels.set_value(t_run))
        with ui.tab_panel(t_run):
            render_run()
        with ui.tab_panel(t_browse):
            render_browse_tables()
        with ui.tab_panel(t_system):
            render_system()


ui.run(
    title="NatureRisk Conversion Console",
    port=8501,
    reload=False,
    favicon="🌿",
    show=False,
)
