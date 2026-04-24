"""Writers — insert/upsert conversion outputs into the existing `nature_risk` schema.

Each save function now:
  1. Compares the incoming file's size against the current row (if any).
  2. If identical → returns ("skipped", <existing_id>) without touching the DB.
  3. Otherwise → archives the existing row into `historical_data` and upserts
     the new payload. Returns ("saved", <id>).

This gives us idempotent writes AND a full audit trail per natural key."""

import json
import os
from pathlib import Path
from psycopg2.extras import Json

from .connection import get_conn
from .history import (
    archive_client_topojson,
    archive_raster_manifest,
    archive_raster_tile,
    archive_topojson_layer,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_topojson(path: str | os.PathLike) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _feature_count(topo: dict) -> int:
    total = 0
    for obj in (topo.get("objects") or {}).values():
        total += len(obj.get("geometries", []) or [])
    return total


# ── topojson_layers (reference data: KBA, IUCN, RAMSAR, WHS, BWS...) ─────────

def save_topojson_layer(
    layer_key: str,
    layer_name: str,
    layer_group: str,
    topojson_path: str | os.PathLike,
    description: str | None = None,
) -> tuple[str, str]:
    """Returns (status, id). Status is 'saved' or 'skipped'."""
    topo = _load_topojson(topojson_path)
    path = Path(topojson_path)
    new_size = path.stat().st_size

    with get_conn() as conn, conn.cursor() as cur:
        # Skip-if-unchanged
        cur.execute(
            "SELECT id, file_size_bytes FROM topojson_layers WHERE layer_key = %s",
            (layer_key,),
        )
        existing = cur.fetchone()
        if existing and existing[1] == new_size:
            return ("skipped", str(existing[0]))

        # Archive existing before overwriting
        if existing:
            archive_topojson_layer(cur, layer_key)

        cur.execute(
            """
            INSERT INTO topojson_layers
                (layer_key, layer_name, layer_group, description,
                 topojson_data, feature_count, bbox, file_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (layer_key) DO UPDATE SET
                layer_name      = EXCLUDED.layer_name,
                layer_group     = EXCLUDED.layer_group,
                description     = EXCLUDED.description,
                topojson_data   = EXCLUDED.topojson_data,
                feature_count   = EXCLUDED.feature_count,
                bbox            = EXCLUDED.bbox,
                file_size_bytes = EXCLUDED.file_size_bytes,
                updated_at      = NOW()
            RETURNING id
            """,
            (
                layer_key, layer_name, layer_group, description,
                Json(topo), _feature_count(topo),
                Json(topo.get("bbox")), new_size,
            ),
        )
        return ("saved", str(cur.fetchone()[0]))


# ── client_topojson (per-client: glencore.topojson, sc_assets.topojson...) ───

def upsert_client(sector_name: str, group_name: str, client_name: str,
                  display_name: str | None = None) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sectors (name) VALUES (%s)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (sector_name,),
        )
        sector_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO client_groups (sector_id, name, display_name)
            VALUES (%s,%s,%s)
            ON CONFLICT (sector_id, name) DO UPDATE SET
                display_name = EXCLUDED.display_name
            RETURNING id
            """,
            (sector_id, group_name, display_name or group_name),
        )
        group_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO clients (group_id, name, display_name)
            VALUES (%s,%s,%s)
            ON CONFLICT (group_id, name) DO UPDATE SET
                display_name = EXCLUDED.display_name
            RETURNING id
            """,
            (group_id, client_name, display_name or client_name),
        )
        return str(cur.fetchone()[0])


def save_client_topojson(
    client_id: str,
    layer_type: str,
    layer_name: str,
    topojson_path: str | os.PathLike,
) -> tuple[str, str]:
    """Returns (status, id)."""
    assert layer_type in ("client_assets", "sc_assets")
    topo = _load_topojson(topojson_path)
    path = Path(topojson_path)
    new_size = path.stat().st_size

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, file_size_bytes FROM client_topojson
            WHERE client_id = %s AND layer_type = %s
            """,
            (client_id, layer_type),
        )
        existing = cur.fetchone()
        if existing and existing[1] == new_size:
            return ("skipped", str(existing[0]))

        if existing:
            archive_client_topojson(cur, client_id, layer_type)

        cur.execute(
            """
            INSERT INTO client_topojson
                (client_id, layer_type, layer_name,
                 topojson_data, feature_count, bbox, file_size_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (client_id, layer_type) DO UPDATE SET
                layer_name      = EXCLUDED.layer_name,
                topojson_data   = EXCLUDED.topojson_data,
                feature_count   = EXCLUDED.feature_count,
                bbox            = EXCLUDED.bbox,
                file_size_bytes = EXCLUDED.file_size_bytes,
                updated_at      = NOW()
            RETURNING id
            """,
            (
                client_id, layer_type, layer_name,
                Json(topo), _feature_count(topo),
                Json(topo.get("bbox")), new_size,
            ),
        )
        return ("saved", str(cur.fetchone()[0]))


# ── raster tiles ────────────────────────────────────────────────────────────

def save_raster_manifest(manifest_path: str, manifest_data: list | dict) -> tuple[str, str]:
    new_count = len(manifest_data) if isinstance(manifest_data, list) else None

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, tile_count FROM raster_manifests WHERE manifest_path = %s",
            (manifest_path,),
        )
        existing = cur.fetchone()
        if existing and existing[1] == new_count:
            # Still do a deep check — manifests can change content with the same count
            cur.execute("SELECT manifest_data FROM raster_manifests WHERE id = %s", (existing[0],))
            old = cur.fetchone()[0]
            if old == manifest_data:
                return ("skipped", str(existing[0]))

        if existing:
            archive_raster_manifest(cur, manifest_path)

        cur.execute(
            """
            INSERT INTO raster_manifests (manifest_path, manifest_data, tile_count)
            VALUES (%s,%s,%s)
            ON CONFLICT (manifest_path) DO UPDATE SET
                manifest_data = EXCLUDED.manifest_data,
                tile_count    = EXCLUDED.tile_count
            RETURNING id
            """,
            (manifest_path, Json(manifest_data), new_count),
        )
        return ("saved", str(cur.fetchone()[0]))


def save_raster_tile(tile_path: str, png_path: str | os.PathLike) -> tuple[str, str]:
    with open(png_path, "rb") as f:
        data = f.read()
    new_size = len(data)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, file_size_bytes FROM raster_tiles WHERE tile_path = %s",
            (tile_path,),
        )
        existing = cur.fetchone()
        if existing and existing[1] == new_size:
            return ("skipped", str(existing[0]))

        if existing:
            archive_raster_tile(cur, tile_path)

        cur.execute(
            """
            INSERT INTO raster_tiles (tile_path, tile_data, file_size_bytes)
            VALUES (%s,%s,%s)
            ON CONFLICT (tile_path) DO UPDATE SET
                tile_data       = EXCLUDED.tile_data,
                file_size_bytes = EXCLUDED.file_size_bytes
            RETURNING id
            """,
            (tile_path, psycopg2_bytes(data), new_size),
        )
        return ("saved", str(cur.fetchone()[0]))


def psycopg2_bytes(data: bytes):
    import psycopg2
    return psycopg2.Binary(data)
