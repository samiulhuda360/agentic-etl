"""The mapping: which source column feeds which warehouse column, and how its values are converted.

A mapping is the one artefact a person reviews. It is plain JSON, versioned on disk, and written by one of three
authors: the rules mapper in this file (no key needed), the model (`agent.py`), or a person editing it in the review
page. Every source column is either used or listed under `ignore` with a reason, so nothing is dropped silently.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .contracts import Column, Contract
from .profile import ColumnProfile, Profile

# The conversions a mapping may name. The compiler turns each into a Polars expression and a DuckDB SQL expression.
VOCABULARY: dict[str, str] = {
    "text": "trim; params.case = upper | lower to normalise",
    "name_last_first": "'Last, First' -> 'First Last'",
    "concat": "join the source columns with a space (e.g. first + last name)",
    "int": "whole number; params as for number",
    "number": "decimal number; params.decimal (',' or '.'), params.thousands, params.factor (e.g. 0.01 for cents)",
    "debit_credit": "two sources [debit, credit] -> credit minus debit",
    "f_to_c": "Fahrenheit number -> Celsius",
    "hours_minutes": "'7:30' -> 7.5",
    "date": "params.format (strftime)",
    "datetime": "params.format (strftime) or 'epoch_s' / 'epoch_ms'",
    "date_from_datetime": "a timestamp column -> its date; params.format",
    "combine_date_time": "two sources [date, time]; params.format for the joined 'date time' text",
    "phone_e164": "phone number -> +<country><number>; params.country (default 64)",
    "const": "params.value: one fixed value for every row",
}


@dataclass
class ColumnMap:
    target: str
    source: list[str]
    transform: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    note: str = ""


@dataclass
class Mapping:
    table: str
    columns: list[ColumnMap]
    ignore: dict[str, str] = field(default_factory=dict)  # source column -> reason it is not loaded
    review: list[str] = field(default_factory=list)  # things a person should check before approving
    author: str = "rules"
    new_table: dict[str, Any] | None = None  # a proposed contract when no existing table fits

    def col(self, target: str) -> ColumnMap | None:
        return next((c for c in self.columns if c.target == target), None)

    def sources(self) -> set[str]:
        return {s for c in self.columns for s in c.source}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["new_table"] is None:
            del d["new_table"]
        return d

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def from_dict(d: dict[str, Any]) -> Mapping:
    cols = []
    for c in d.get("columns", []):
        src = c.get("source", [])
        cols.append(
            ColumnMap(
                target=c["target"],
                source=[src] if isinstance(src, str) else list(src or []),
                transform=c.get("transform", "text"),
                params=dict(c.get("params") or {}),
                confidence=float(c.get("confidence", 1.0)),
                note=str(c.get("note", "")),
            )
        )
    ignore = d.get("ignore") or {}
    if isinstance(ignore, list):
        ignore = {k: "not needed" for k in ignore}
    return Mapping(
        table=d["table"],
        columns=cols,
        ignore=dict(ignore),
        review=list(d.get("review") or []),
        author=d.get("author", "rules"),
        new_table=d.get("new_table"),
    )


def problems(m: Mapping, contract: Contract, columns: list[str]) -> list[str]:
    """Structural checks run before any code is generated. `columns` are the file's column names."""
    out = []
    names = set(columns)
    for c in m.columns:
        if c.target not in contract.names:
            out.append(f"'{c.target}' is not a column of {contract.table}")
        if c.transform not in VOCABULARY:
            out.append(f"{c.target}: unknown transform '{c.transform}'")
        for s in c.source:
            if s not in names:
                out.append(f"{c.target}: source column '{s}' is not in the file")
        if c.transform != "const" and not c.source:
            out.append(f"{c.target}: no source column")
    # A required column filled with a constant empty value is a missing source in disguise.
    empty = {c.target for c in m.columns if c.transform == "const" and c.params.get("value") in (None, "")}
    mapped = {c.target for c in m.columns} - empty
    for col in contract.columns:
        if col.name not in mapped and not col.nullable:
            out.append(f"required column '{col.name}' has no source")
    loose = [c for c in columns if not c.startswith("@") and c not in m.sources() and c not in m.ignore]
    if loose:
        out.append("source columns neither loaded nor ignored: " + ", ".join(loose))
    return out


# ---------------------------------------------------------------- name matching

STOP = {"the", "of", "no", "number", "nr", "items", "lines", "entries", "payload", "data", "stats", "contact", "org"}


def tokens(name: str) -> list[str]:
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name.lstrip("@"))
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^a-z0-9#%]+", name.lower()) if t]


def name_score(source: str, col: Column) -> float:
    st = [t for t in tokens(source) if t not in STOP] or tokens(source)
    s = " ".join(st)
    best = 0.0
    for alias in [col.name.replace("_", " ")] + col.aliases:
        at = tokens(alias)
        a = " ".join(at)
        if not a:
            continue
        if s == a:
            return 1.0
        if set(at) <= set(st):
            best = max(best, 0.85 if len(at) > 1 else 0.7)
        inter = len(set(at) & set(st))
        if inter:
            best = max(best, 0.6 * inter / len(set(at) | set(st)) + 0.25)
        best = max(best, 0.9 * difflib.SequenceMatcher(None, s, a).ratio() - 0.1)
    return best


def compatible(col: Column, p: ColumnProfile) -> bool:
    k = p.kind
    if col.type == "string":
        if col.format == "e164":
            return k == "phone"
        if "email" in col.name:
            return k == "email"
        return k in ("code", "text", "email", "phone", "int")
    if col.type == "int":
        return k == "int"
    if col.type in ("decimal", "float"):
        return k in ("int", "decimal", "money", "percent", "hours_minutes")
    if col.type == "date":
        return k in ("date", "datetime")
    if col.type == "datetime":
        return k == "datetime"
    if col.type == "bool":
        return k == "bool"
    return False


def conversion(col: Column, p: ColumnProfile) -> tuple[str, dict[str, Any], str]:
    """Pick the transform and its parameters for one matched pair, from the column's profile."""
    params: dict[str, Any] = {}
    note = ""
    if col.type == "string":
        if col.format == "e164":
            return "phone_e164", {"country": "64"}, ""
        if '"First Last"' in col.description and any(", " in s for s in p.samples):
            return "name_last_first", {}, "names are written 'Last, First'"
        if col.case:
            params["case"] = col.case
        return "text", params, note
    if col.type in ("int", "decimal", "float"):
        if p.kind == "hours_minutes":
            return "hours_minutes", {}, "hours are written as h:mm"
        if p.number:
            if p.number.decimal == ",":
                params["decimal"] = ","
            if p.number.thousands:
                params["thousands"] = p.number.thousands
        unit = (p.unit or "").lower()
        if col.name.endswith("_c") and (unit == "f" or (p.number and p.number.unit == "°F")):
            return "f_to_c", params, "the file is in Fahrenheit"
        if unit == "cents":
            params["factor"] = 0.01
            note = "amounts are in cents"
        if unit == "mv" and col.name.endswith("_v"):
            params["factor"] = 0.001
            note = "millivolts to volts"
        return ("int" if col.type == "int" else "number"), params, note
    if col.type == "date":
        if p.kind == "datetime":
            return "date_from_datetime", {"format": p.format}, "the file has a timestamp; the date part is kept"
        note = f"also fits {', '.join(p.ambiguous)}; check" if p.ambiguous else ""
        return "date", {"format": p.format}, note
    if col.type == "datetime":
        return "datetime", {"format": p.format}, ""
    return "text", {}, ""


def rules_mapping(prof: Profile, contract: Contract) -> tuple[Mapping, float]:
    """Map by alias and name similarity, gated by type compatibility. Returns the mapping and a fit score."""
    pairs = []
    for col in contract.columns:
        for p in prof.columns:
            sc = name_score(p.name, col)
            if sc >= 0.5 and compatible(col, p):
                pairs.append((sc, col.name, p.name))
    pairs.sort(key=lambda x: -x[0])
    used_t: set[str] = set()
    used_s: set[str] = set()
    cols: list[ColumnMap] = []
    for sc, t, s in pairs:
        if t in used_t or s in used_s:
            continue
        col, p = contract.col(t), prof.col(s)
        tr, params, note = conversion(col, p)
        cols.append(ColumnMap(t, [s], tr, params, round(sc, 2), note))
        used_t.add(t)
        used_s.add(s)
    review: list[str] = []
    for col in contract.columns:
        if col.name in used_t:
            continue
        derived = _derive(col, prof, used_s)
        if derived:
            cols.append(derived)
            used_t.add(col.name)
            used_s.update(derived.source)
    for c in cols:
        if c.note.startswith("also fits"):
            review.append(f"{c.target}: date format {c.params.get('format')} {c.note}")
    order = {n: i for i, n in enumerate(contract.names)}
    cols.sort(key=lambda c: order[c.target])
    ignore = {
        p.name: "no matching column in the table"
        for p in prof.columns
        if p.name not in used_s and not p.name.startswith("@")
    }
    for col in contract.columns:
        if col.name not in used_t:
            review.append(f"no source found for '{col.name}'")
    score = sum(c.confidence for c in cols) / max(1, len(contract.columns))
    return Mapping(contract.table, cols, ignore, review, "rules"), score


def _derive(col: Column, prof: Profile, used: set[str]) -> ColumnMap | None:
    free = [p for p in prof.columns if p.name not in used]
    toks = {p.name: set(tokens(p.name)) for p in free}
    if col.type in ("decimal", "float"):
        debit = next((p for p in free if toks[p.name] & {"debit", "withdrawal", "dr"}), None)
        credit = next((p for p in free if toks[p.name] & {"credit", "deposit", "cr"}), None)
        if debit and credit and col.reconcile:
            params: dict[str, Any] = {}
            if debit.number and debit.number.decimal == ",":
                params["decimal"] = ","
            if debit.number and debit.number.thousands:
                params["thousands"] = debit.number.thousands
            return ColumnMap(
                col.name, [debit.name, credit.name], "debit_credit", params, 0.7, "separate debit and credit columns"
            )
    if col.type == "string" and "name" in col.name:
        first = next((p for p in free if toks[p.name] & {"first", "given", "firstname"}), None)
        last = next((p for p in free if toks[p.name] & {"last", "surname", "family", "lastname"}), None)
        if first and last:
            return ColumnMap(col.name, [first.name, last.name], "concat", {}, 0.7, "first and last name joined")
    if col.type == "datetime":
        d = next((p for p in free if p.kind == "date"), None)
        t = next((p for p in free if p.kind == "time"), None)
        if d and t and d.format:
            return ColumnMap(
                col.name,
                [d.name, t.name],
                "combine_date_time",
                {"format": f"{d.format} {t.format}"},
                0.6,
                "date and time are in separate columns",
            )
    if col.name == "currency":
        for p in prof.columns:
            if p.number and p.number.currency:
                return ColumnMap(
                    col.name,
                    [],
                    "const",
                    {"value": p.number.currency},
                    0.6,
                    f"implied by the '{p.number.symbol}' written with the prices",
                )
    return None


def route(
    prof: Profile, catalogue: dict[str, Contract], threshold: float = 0.45
) -> tuple[str | None, dict[str, float]]:
    """Pick the existing table this file fits best, or None when nothing fits well enough."""
    scores = {name: rules_mapping(prof, c)[1] for name, c in catalogue.items()}
    best = max(scores, key=lambda k: scores[k]) if scores else None
    if best is None or scores[best] < threshold:
        return None, scores
    return best, scores


def snake(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", " ".join(tokens(name)).replace("#", "no").replace("%", "pct"))
    return s.strip("_") or "column"


KIND_TYPES = {
    "int": "int",
    "decimal": "float",
    "money": "decimal",
    "percent": "float",
    "date": "date",
    "datetime": "datetime",
    "bool": "bool",
    "hours_minutes": "float",
}


def propose_new_table(prof: Profile, name: str) -> tuple[Contract, Mapping]:
    """A new table shaped like the file: snake_case names, types from the profile, the first candidate key."""
    from .contracts import from_dict

    cols, maps = [], []
    for p in prof.columns:
        if p.name.startswith("@") or p.kind == "empty":
            continue
        t = KIND_TYPES.get(p.kind, "string")
        n = snake(p.name)
        cols.append({"name": n, "type": t, "nullable": p.nulls > 0, "description": f"From '{p.name}'."})
    contract = from_dict(
        {
            "table": name,
            "description": f"Proposed from {prof.file}.",
            "key": [snake(k) for k in (prof.keys[0] if prof.keys else [])],
            "columns": cols,
        },
        new=True,
    )
    for p in prof.columns:
        if p.name.startswith("@") or p.kind == "empty":
            continue
        col = contract.col(snake(p.name))
        tr, params, note = conversion(col, p)
        maps.append(ColumnMap(col.name, [p.name], tr, params, 1.0, note))
    m = Mapping(
        name,
        maps,
        {},
        [f"new table '{name}' proposed: check names, types and key"],
        "rules",
        new_table=contract.to_dict(),
    )
    return contract, m
