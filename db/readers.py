"""Readers — list/preview rows for the Browse page."""

from .connection import get_conn


def table_row_counts() -> list[tuple[str, int]]:
    """Row counts for tables the UI manages or references."""
    tables = [
        "sectors", "client_groups", "clients",
        "client_assets", "client_topojson", "topojson_layers",
        "raster_manifests", "raster_tiles",
        "heatmap_data", "radar_data", "grid_data",
    ]
    out: list[tuple[str, int]] = []
    with get_conn() as conn, conn.cursor() as cur:
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                out.append((t, cur.fetchone()[0]))
            except Exception:
                out.append((t, -1))
    return out


def list_topojson_layers() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, layer_key, layer_name, layer_group, feature_count,
                   file_size_bytes, bbox, updated_at
            FROM topojson_layers
            ORDER BY layer_group, layer_key
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_client_topojsons() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ct.id, c.name AS client_name, c.display_name,
                   cg.display_name AS group_name, s.name AS sector_name,
                   ct.layer_type, ct.layer_name, ct.feature_count,
                   ct.file_size_bytes, ct.bbox, ct.updated_at
            FROM client_topojson ct
            JOIN clients c       ON c.id = ct.client_id
            JOIN client_groups cg ON cg.id = c.group_id
            JOIN sectors s        ON s.id = cg.sector_id
            ORDER BY s.name, cg.display_name, c.display_name, ct.layer_type
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_topojson_data(row_id: str, table: str) -> dict | None:
    """Fetch the raw topojson JSONB for preview. table ∈ {topojson_layers, client_topojson}."""
    assert table in ("topojson_layers", "client_topojson")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT topojson_data FROM {table} WHERE id = %s", (row_id,))
        row = cur.fetchone()
        return row[0] if row else None


def list_raster_manifests() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, manifest_path, tile_count, created_at
            FROM raster_manifests
            ORDER BY manifest_path
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_raster_tiles(limit: int = 200) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tile_path, file_size_bytes, created_at
            FROM raster_tiles
            ORDER BY tile_path
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_hierarchy() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.name AS sector, cg.display_name AS group_name,
                   c.display_name AS client, c.is_active
            FROM clients c
            JOIN client_groups cg ON cg.id = c.group_id
            JOIN sectors s        ON s.id = cg.sector_id
            ORDER BY s.name, cg.display_name, c.display_name
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
