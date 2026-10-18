"""Data tests, built from the table contract, the mapping and the file profile.

Two families, because they call for different actions:

* **Fidelity tests** check the *code*: right columns and types, every row kept, no value turned into a null by a
  failed parse, totals that reconcile with an independent parse of the source. A failure means the transform is
  wrong; it is fed back to the model for repair and the version is never promoted.
* **Quality tests** check the *data*: required values present, unique keys, allowed codes, plausible ranges. A
  failure on a few rows means the file carries bad records; those rows go to quarantine with the reason, the rest
  load. A failure on most rows looks like a code problem (say, codes not upper-cased) and is treated as one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl

from .contracts import Contract
from .mapping import Mapping
from .profile import NumberStyle, Profile, is_null, parse_number

SYSTEMATIC = 0.2  # a quality failure on more than this share of rows is treated as a code problem


@dataclass
class TestResult:
    name: str
    family: str  # fidelity | quality
    passed: bool
    detail: str = ""
    rows: list[int] = field(default_factory=list)  # failing row positions
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rows"] = d["rows"][:50]
        return d


@dataclass
class Report:
    tests: list[TestResult]
    rows_in: int

    @property
    def fidelity_failures(self) -> list[TestResult]:
        return [t for t in self.tests if t.family == "fidelity" and not t.passed]

    @property
    def systematic(self) -> list[TestResult]:
        limit = max(2, int(SYSTEMATIC * self.rows_in))
        return [t for t in self.tests if t.family == "quality" and not t.passed and len(t.rows) > limit]

    @property
    def code_ok(self) -> bool:
        return not self.fidelity_failures and not self.systematic

    def quarantine(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for t in self.tests:
            if t.family == "quality" and not t.passed:
                for r in t.rows:
                    out.setdefault(r, []).append(t.name)
        return out

    def summary(self) -> dict[str, int]:
        return {"passed": sum(t.passed for t in self.tests), "failed": sum(not t.passed for t in self.tests)}


def _examples(
    raw: pl.DataFrame, out: pl.DataFrame | None, rows: list[int], sources: list[str], target: str | None
) -> list[dict[str, Any]]:
    ex = []
    for r in rows[:3]:
        d: dict[str, Any] = {"row": r}
        for s in sources:
            if s in raw.columns:
                d[f"raw {s}"] = raw[s][r]
        if out is not None and target and target in out.columns:
            v = out[target][r]
            d[f"output {target}"] = v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v
        ex.append(d)
    return ex


def _missing(s: pl.Series) -> list[bool]:
    """Null, or (for text) blank: an empty string is not a value, whatever the code returned."""
    if s.dtype == pl.String:
        return (s.is_null() | (s.str.strip_chars() == "")).to_list()
    return s.is_null().to_list()


def _sources(m: Mapping, target: str) -> list[str]:
    c = m.col(target)
    return c.source if c else []


def _reference(m: Mapping, target: str, raw: pl.DataFrame, prof: Profile) -> list[float | None] | None:
    """Parse the mapped source independently of the generated code (profile number style + mapping factor)."""
    c = m.col(target)
    if c is None or not c.source:
        return None

    def style(col: str) -> NumberStyle:
        try:
            st = prof.col(col).number
        except KeyError:
            st = None
        return st or NumberStyle()

    def vals(col: str) -> list[float | None]:
        st = style(col)
        return [parse_number(v, st) for v in raw[col].to_list()]

    factor = float(c.params.get("factor", 1.0) or 1.0)
    if c.transform in ("number", "int"):
        return [None if v is None else v * factor for v in vals(c.source[0])]
    if c.transform == "debit_credit":
        d, cr = vals(c.source[0]), vals(c.source[1])
        return [(b or 0.0) - (a or 0.0) for a, b in zip(d, cr, strict=True)]
    if c.transform == "hours_minutes":
        out: list[float | None] = []
        for v in raw[c.source[0]].to_list():
            if v is None or is_null(v) or ":" not in v:
                out.append(None)
                continue
            h, mi = v.strip().split(":", 1)
            out.append(int(h) + int(mi) / 60)
        return out
    return None


def build_and_run(
    out: pl.DataFrame | None, raw: pl.DataFrame, contract: Contract, m: Mapping, prof: Profile, error: str | None = None
) -> Report:
    n = raw.height
    tests: list[TestResult] = []
    if out is None:
        return Report([TestResult("transform runs", "fidelity", False, error or "no output")], n)
    tests.append(TestResult("transform runs", "fidelity", True))

    want = contract.schema()
    got = dict(out.schema)
    missing = [c for c in want if c not in got]
    extra = [c for c in got if c not in want]
    wrong = [f"{c} is {got[c]}, expected {want[c]}" for c in want if c in got and got[c] != want[c]]
    detail = "; ".join(
        ([f"missing columns: {', '.join(missing)}"] if missing else [])
        + ([f"unexpected columns: {', '.join(extra)}"] if extra else [])
        + wrong
    )
    tests.append(TestResult("schema matches the table", "fidelity", not detail, detail))
    if missing:
        return Report(tests, n)

    ok = out.height == n
    tests.append(
        TestResult(
            "row count",
            "fidelity",
            ok,
            "" if ok else f"{n} rows in the file, {out.height} rows out: rows were dropped or added",
        )
    )
    if not ok:
        return Report(tests, n)

    # No new nulls: a value present in the source must not become null in the output.
    for c in m.columns:
        if not c.source or c.target not in out.columns:
            continue
        present = [any(not is_null(raw[s][i]) for s in c.source if s in raw.columns) for i in range(n)]
        if c.transform == "combine_date_time":
            present = [all(not is_null(raw[s][i]) for s in c.source) for i in range(n)]
        outnull = _missing(out[c.target])
        bad = [i for i in range(n) if present[i] and outnull[i]]
        tests.append(
            TestResult(
                f"no values lost: {c.target}",
                "fidelity",
                not bad,
                f"{len(bad)} values became null" if bad else "",
                bad,
                _examples(raw, out, bad, c.source, c.target),
            )
        )

    # Reconciliation: totals agree with an independent parse of the source.
    for col in contract.columns:
        if not col.reconcile or col.name not in out.columns:
            continue
        ref = _reference(m, col.name, raw, prof)
        if ref is None:
            continue
        values = out[col.name].cast(pl.Float64, strict=False).to_list()
        want_sum = sum(v for v in ref if v is not None)
        got_sum = sum(v for v in values if v is not None)
        tol = max(0.01, 0.006 * n) if col.type == "decimal" else max(0.01, 1e-6 * n)
        okr = abs(want_sum - got_sum) <= tol
        bad = [
            i
            for i in range(n)
            if ref[i] is not None and values[i] is not None and abs(ref[i] - values[i]) > max(col.tol(), 0.006)
        ]
        tests.append(
            TestResult(
                f"total reconciles: {col.name}",
                "fidelity",
                okr,
                "" if okr else f"source total {want_sum:,.2f}, loaded total {got_sum:,.2f}",
                bad,
                _examples(raw, out, bad, _sources(m, col.name), col.name),
            )
        )

    # Quality tests: rows that break the contract.
    for col in contract.columns:
        if col.name not in out.columns:
            continue
        s = out[col.name]
        cm = m.col(col.name)
        src = cm.source if cm else []
        if not col.nullable:
            bad = [i for i, v in enumerate(_missing(s)) if v]
            tests.append(
                TestResult(
                    f"not null: {col.name}",
                    "quality",
                    not bad,
                    f"{len(bad)} missing" if bad else "",
                    bad,
                    _examples(raw, out, bad, src, col.name),
                )
            )
        if col.accepted:
            vals = s.cast(pl.String, strict=False).to_list()
            bad = [i for i, v in enumerate(vals) if v is not None and v not in col.accepted]
            tests.append(
                TestResult(
                    f"allowed values: {col.name}",
                    "quality",
                    not bad,
                    f"{len(bad)} not in {col.accepted}" if bad else "",
                    bad,
                    _examples(raw, out, bad, src, col.name),
                )
            )
        if col.range:
            lo, hi = col.range
            if col.type in ("date", "datetime"):
                lo_d, hi_d = dt.date.fromisoformat(str(lo)), dt.date.fromisoformat(str(hi))
                vals_d = s.to_list()
                bad = [
                    i
                    for i, v in enumerate(vals_d)
                    if v is not None and not (lo_d <= (v.date() if isinstance(v, dt.datetime) else v) <= hi_d)
                ]
            else:
                vals_f = s.cast(pl.Float64, strict=False).to_list()
                bad = [i for i, v in enumerate(vals_f) if v is not None and not (float(lo) <= v <= float(hi))]
            tests.append(
                TestResult(
                    f"in range: {col.name} [{lo} .. {hi}]",
                    "quality",
                    not bad,
                    f"{len(bad)} outside the range" if bad else "",
                    bad,
                    _examples(raw, out, bad, src, col.name),
                )
            )
    if contract.key and all(k in out.columns for k in contract.key):
        keys = out.select(contract.key).rows()
        seen: dict[tuple[Any, ...], int] = {}
        bad = []
        for i, k in enumerate(keys):
            if any(v is None for v in k):
                continue
            if k in seen:
                bad.append(i)
            else:
                seen[k] = i
        tests.append(
            TestResult(
                f"unique key: {', '.join(contract.key)}",
                "quality",
                not bad,
                f"{len(bad)} rows repeat an earlier key" if bad else "",
                bad,
                _examples(raw, out, bad, [], None),
            )
        )
    return Report(tests, n)
