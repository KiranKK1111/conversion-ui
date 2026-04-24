"""
Centralized configuration loaded from .env.

Resolves `MODEL_TUNE_DIR` automatically to the sibling `model-tune/` folder
so the app works out of the box when cloned into the standard layout.
"""

import os
from pathlib import Path
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


class Settings:
    DB_HOST: str = _get("DB_HOST", "localhost")
    DB_PORT: int = _get_int("DB_PORT", 5432)
    DB_NAME: str = _get("DB_NAME", "postgres")
    DB_USER: str = _get("DB_USER", "postgres")
    DB_PASSWORD: str = _get("DB_PASSWORD", "")
    DB_SCHEMA: str = _get("DB_SCHEMA", "nature_risk")

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def MODEL_TUNE_DIR(self) -> Path:
        raw = _get("MODEL_TUNE_DIR", "")
        if raw:
            return Path(raw).expanduser().resolve()
        # Fallback: assume sibling folder layout
        return (HERE.parent / "model-tune").resolve()

    @property
    def WORK_DIR(self) -> Path:
        raw = _get("WORK_DIR", "./_work")
        p = Path(raw)
        if not p.is_absolute():
            p = (HERE / raw).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
