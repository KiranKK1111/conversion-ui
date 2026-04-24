"""Tiny psycopg2 wrapper — context-managed connection, sets search_path."""

from contextlib import contextmanager
import psycopg2

from config import settings


@contextmanager
def get_conn():
    """Yield a psycopg2 connection with search_path pre-set to nature_risk."""
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {settings.DB_SCHEMA}, public")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_connection() -> tuple[bool, str]:
    """Return (ok, message) — used by the System Status page."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version(), current_database(), current_schema()")
                ver, db, schema = cur.fetchone()
        return True, f"Connected · db={db} · schema={schema}\n{ver}"
    except Exception as e:
        return False, str(e)
