from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_etl.contracts import Contract, catalogue
from agentic_etl.paths import DATASETS


@pytest.fixture(scope="session")
def manifest() -> dict[str, Any]:
    return json.loads((DATASETS / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in manifest["files"]}


@pytest.fixture(scope="session")
def cat() -> dict[str, Contract]:
    return catalogue()


def path(rel: str) -> Path:
    return DATASETS / rel
