"""Where things live. Every location can be moved with an environment variable."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def contracts_dir() -> Path:
    """The warehouse catalogue: one YAML contract per existing table."""
    return Path(os.getenv("ETL_CONTRACTS", ROOT / "contracts"))


def home() -> Path:
    """The workspace: warehouse.duckdb, mapping and code versions, run records."""
    return Path(os.getenv("ETL_HOME", ROOT / ".etl"))


def cache_dir() -> Path:
    """Recorded model replies, replayed offline (committed so CI never calls a model)."""
    return Path(os.getenv("ETL_CACHE", ROOT / "cache"))


DATASETS = ROOT / "datasets"
