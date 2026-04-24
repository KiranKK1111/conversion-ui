"""Historical data tracking.

When a conversion re-runs and produces a file that's already in the database,
we want to:

  1. Detect "no changes" (same file_size_bytes) → skip the write entirely.
  2. Detect a change → copy the existing row into `historical_data` before
     the upsert replaces it. This gives a full audit trail per natural key.

The `historical_data` table is auto-created on first use — the schema file
itself lives here rather than in the existing `backend/database/*.sql` migrations
so we don't touch that project's implementation."""

from __future__ import annotations

from typing import Literal
from psycopg2.extras import Json

from .connection import get_conn


HIST_DDL = """
CREATE TABLE IF NOT EXISTS historical_data (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_table     VARCHAR(100) NOT NULL,
    source_id        UUID,
    source_key       VARCHAR(600) NOT NULL,
    payload_json     JSONB,
    payload_bytes    BYTEA,
    file_size_bytes  BIGINT,
    metadata         JSONB,
    archived_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hist_source      ON historical_data(source_table, source_key);
CREATE INDEX IF NOT EXISTS idx_hist_archived_at ON historical_data(archived_at DESC);
"""


def ensure_history_schema() -> None:
    """Run once at app startup. Safe to call multiple times."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(HIST_DDL)


# ── archive helpers — each runs inside the caller's transaction ─────────────

SaveStatus = Literal["saved", "skipped"]


def archive_topojson_layer(cur, layer_key: str) -> int | None:
    """If a row with this layer_key exists, copy it into historical_data and
    return its file_size_bytes. Otherwise return None."""
    cur.execute(
        """
        SELECT id, file_size_bytes, topojson_data, layer_name, layer_group,
               description, feature_count, bbox
        FROM topojson_layers WHERE layer_key = %s
        """,
        (layer_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    old_id, old_size, old_data, ln, lg, desc, fc, bbox = row
    cur.execute(
        """
        INSERT INTO historical_data
            (source_table, source_id, source_key,
             payload_json, file_size_bytes, metadata)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            "topojson_layers", old_id, layer_key,
            Json(old_data), old_size,
            Json({"layer_name": ln, "layer_group": lg, "description": desc,
                  "feature_count": fc, "bbox": bbox}),
        ),
    )
    return old_size


def archive_client_topojson(cur, client_id: str, layer_type: str) -> int | None:
    cur.execute(
        """
        SELECT id, file_size_bytes, topojson_data, layer_name,
               feature_count, bbox
        FROM client_topojson
        WHERE client_id = %s AND layer_type = %s
        """,
        (client_id, layer_type),
    )
    row = cur.fetchone()
    if not row:
        return None
    old_id, old_size, old_data, ln, fc, bbox = row
    cur.execute(
        """
        INSERT INTO historical_data
            (source_table, source_id, source_key,
             payload_json, file_size_bytes, metadata)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            "client_topojson", old_id, f"{client_id}:{layer_type}",
            Json(old_data), old_size,
            Json({"client_id": client_id, "layer_type": layer_type,
                  "layer_name": ln, "feature_count": fc, "bbox": bbox}),
        ),
    )
    return old_size


def archive_raster_tile(cur, tile_path: str) -> int | None:
    cur.execute(
        """
        SELECT id, file_size_bytes, tile_data
        FROM raster_tiles WHERE tile_path = %s
        """,
        (tile_path,),
    )
    row = cur.fetchone()
    if not row:
        return None
    old_id, old_size, old_bytes = row
    cur.execute(
        """
        INSERT INTO historical_data
            (source_table, source_id, source_key,
             payload_bytes, file_size_bytes, metadata)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            "raster_tiles", old_id, tile_path,
            bytes(old_bytes) if old_bytes is not None else None, old_size,
            Json({"tile_path": tile_path}),
        ),
    )
    return old_size


def archive_raster_manifest(cur, manifest_path: str) -> int | None:
    cur.execute(
        """
        SELECT id, tile_count, manifest_data
        FROM raster_manifests WHERE manifest_path = %s
        """,
        (manifest_path,),
    )
    row = cur.fetchone()
    if not row:
        return None
    old_id, tile_count, old_data = row
    cur.execute(
        """
        INSERT INTO historical_data
            (source_table, source_id, source_key,
             payload_json, file_size_bytes, metadata)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            "raster_manifests", old_id, manifest_path,
            Json(old_data), None,
            Json({"manifest_path": manifest_path, "tile_count": tile_count}),
        ),
    )
    # Manifests don't have a file_size_bytes column in the source table,
    # so we return tile_count as a proxy for change detection
    return tile_count


def list_history(limit: int = 200) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_table, source_key, file_size_bytes, archived_at
            FROM historical_data
            ORDER BY archived_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
