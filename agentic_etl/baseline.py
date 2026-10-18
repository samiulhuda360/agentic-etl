"""The baseline a busy analyst would write first: pandas readers with default settings, plus naive name matching.

* CSV-like files: `pd.read_csv(path)`; `.tsv` and `.psv` get their obvious separator; a `.txt` that reads as one
  column is retried with `pd.read_fwf(path)`.
* Excel: `pd.read_excel(path)` (first sheet, first row as header).
* JSON: `pd.read_json(path)`, and `lines=True` for `.ndjson`.
* Columns: a target column takes the source column whose normalised name is equal, else the closest name by
  `difflib` (cutoff 0.6). Values go through `pd.to_datetime(errors="coerce")` / `pd.to_numeric(errors="coerce")`.

Nothing is tested, so nothing is quarantined, and a value that fails to parse silently becomes empty.
"""

from __future__ import annotations

import difflib
import re
import warnings
from pathlib import Path

import pandas as pd
import polars as pl

from .contracts import Contract


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def read_default(path: Path) -> pd.DataFrame:
    s = path.suffix.lower()
    if s in (".xlsx", ".xlsm"):
        return pd.read_excel(path)
    if s == ".json":
        return pd.read_json(path)
    if s in (".ndjson", ".jsonl"):
        return pd.read_json(path, lines=True)
    if s == ".tsv":
        return pd.read_csv(path, sep="\t")
    if s == ".psv":
        return pd.read_csv(path, sep="|")
    df = pd.read_csv(path)
    if s == ".txt" and df.shape[1] == 1:
        df = pd.read_fwf(path)
    return df


def load(path: Path, contract: Contract) -> pl.DataFrame:
    """Raises when pandas cannot read the file at all (that failure is loud, so it is not silent loss)."""
    df = read_default(path)
    names = {_norm(c): c for c in df.columns}
    out: dict[str, list[object]] = {}
    for col in contract.columns:
        src = names.get(col.name) or next(iter(difflib.get_close_matches(col.name, list(names), n=1, cutoff=0.6)), None)
        if src is None:
            out[col.name] = [None] * len(df)
            continue
        s = df[names[src]] if src in names else df[src]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if col.type in ("int", "decimal", "float"):
                v = pd.to_numeric(s, errors="coerce")
                v = v.round(2) if col.type == "decimal" else v
                out[col.name] = [None if pd.isna(x) else float(x) for x in v]
            elif col.type == "date":
                d = pd.to_datetime(s, errors="coerce")
                out[col.name] = [None if pd.isna(x) else x.date() for x in d]
            elif col.type == "datetime":
                d = pd.to_datetime(s, errors="coerce")
                out[col.name] = [None if pd.isna(x) else x.to_pydatetime().replace(tzinfo=None) for x in d]
            else:
                out[col.name] = [None if pd.isna(x) else str(x).strip() for x in s]
    return pl.DataFrame(out, strict=False, infer_schema_length=None)
