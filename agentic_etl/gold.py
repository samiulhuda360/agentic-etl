"""Compare a loaded table with its hand-checked gold table: exact match and cell-level accuracy."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .contracts import Column, Contract


@dataclass
class Score:
    exact: bool
    cells_right: int
    cells_total: int
    missing_rows: int
    extra_rows: int
    wrong: dict[str, int] = field(default_factory=dict)  # column -> wrong cells (rows matched by key)
    position_right: int = 0  # cells equal when rows are matched by position instead of by key

    @property
    def accuracy(self) -> float:
        """Cell accuracy with rows matched by position, so a mangled key column does not hide right values."""
        return self.position_right / self.cells_total if self.cells_total else 1.0


def _norm(col: Column, v: Any) -> Any:
    if v is None or v == "":
        return None
    if col.type in ("decimal", "float", "int"):
        try:
            return float(v)
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _same(col: Column, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= col.tol() + 1e-9
    return bool(a == b)


def read_gold(path: Path) -> pl.DataFrame:
    return pl.read_csv(path, infer_schema=False)


def compare(out: pl.DataFrame | None, gold: pl.DataFrame, contract: Contract) -> Score:
    cols = contract.columns
    ncols = len(cols)
    gold_rows = [{c.name: _norm(c, r.get(c.name)) for c in cols} for r in gold.to_dicts()]
    if out is None:
        return Score(False, 0, len(gold_rows) * ncols, len(gold_rows), 0, {}, 0)
    present = [c for c in cols if c.name in out.columns]
    out_rows = [{c.name: _norm(c, r.get(c.name)) for c in present} for r in out.to_dicts()]
    key = contract.key or [c.name for c in cols]

    def k(r: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(r.get(x) for x in key)

    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    extra = 0
    for r in out_rows:
        if k(r) in by_key:
            extra += 1
        else:
            by_key[k(r)] = r
    right = 0
    missing = 0
    wrong: dict[str, int] = {}
    matched = set()
    for g in gold_rows:
        o = by_key.get(k(g))
        if o is None:
            missing += 1
            continue
        matched.add(k(g))
        for c in cols:
            if _same(c, g[c.name], o.get(c.name)):
                right += 1
            else:
                wrong[c.name] = wrong.get(c.name, 0) + 1
    extra += len(set(by_key) - matched)
    total = max(len(gold_rows), len(out_rows)) * ncols
    pos = 0
    for g, o in zip(gold_rows, out_rows, strict=False):
        pos += sum(_same(c, g[c.name], o.get(c.name)) for c in cols)
    exact = missing == 0 and extra == 0 and not wrong and len(present) == ncols
    return Score(exact, right, total, missing, extra, wrong, pos)
