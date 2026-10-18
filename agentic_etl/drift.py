"""Schema drift: compare a new file with the fingerprint of the file its approved mapping was made for.

Detects read changes (delimiter, encoding, format), removed, added, renamed, reordered and split columns, and
value-format or unit changes in columns that are still there. Then proposes an updated mapping (by rules, or by
the model) and re-runs the data tests. A required column with no source blocks the load; a new column with no
place in the table is never dropped quietly: it is put in `ignore` *and* flagged for a person to confirm.
"""

from __future__ import annotations

import copy
import difflib
from typing import Any

from .contracts import Contract
from .mapping import ColumnMap, Mapping, _derive, conversion, tokens
from .profile import Profile


def fingerprint(prof: Profile) -> dict[str, Any]:
    return {"file": prof.file, "read": prof.read, "columns": [c.brief() for c in prof.columns]}


def _name_sim(a: str, b: str) -> float:
    ta, tb = " ".join(tokens(a)), " ".join(tokens(b))
    if ta and ta == tb:
        return 1.0
    return difflib.SequenceMatcher(None, ta, tb).ratio()


def _shape(c: dict[str, Any]) -> tuple[Any, ...]:
    n = c.get("number") or {}
    return (c.get("kind"), c.get("format"), n.get("decimal", "."), n.get("symbol"), n.get("unit"))


def detect(old: dict[str, Any], prof: Profile) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    o_read, n_read = old.get("read", {}), prof.read
    for k in ("format", "delimiter", "encoding", "header_rows"):
        if o_read.get(k) != n_read.get(k):
            changes.append({"kind": "read", "what": k, "old": o_read.get(k), "new": n_read.get(k)})
    ocols = {c["name"]: c for c in old["columns"] if not c["name"].startswith("@")}
    ncols = {c.name: c.brief() for c in prof.columns if not c.name.startswith("@")}
    oorder, norder = list(ocols), list(ncols)
    removed = [c for c in oorder if c not in ncols]
    added = [c for c in norder if c not in ocols]

    # One column replaced by several that share its name (e.g. Name -> First Name + Last Name): a split.
    for r in list(removed):
        rt = set(tokens(r))
        parts = [a for a in added if rt and rt <= set(tokens(a)) and ncols[a].get("kind") == ocols[r].get("kind")]
        if len(parts) >= 2:
            changes.append({"kind": "split", "old": r, "new": parts})
            removed.remove(r)
            added = [a for a in added if a not in parts]

    # Pair removed and added columns that are the same field under a new name.
    pairs = []
    for r in removed:
        for a in added:
            ro, an = ocols[r], ncols[a]
            same_kind = ro.get("kind") == an.get("kind")
            pos = 1 - min(1.0, abs(oorder.index(r) / max(1, len(oorder)) - norder.index(a) / max(1, len(norder))) * 3)
            score = 0.55 * _name_sim(r, a) + 0.3 * same_kind + 0.15 * pos
            if score >= 0.6:
                pairs.append((score, r, a))
    pairs.sort(key=lambda x: -x[0])
    renamed: dict[str, str] = {}
    for _score, r, a in pairs:
        if r not in renamed and a not in renamed.values():
            renamed[r] = a
    for r, a in renamed.items():
        changes.append({"kind": "renamed", "old": r, "new": a})
    removed = [r for r in removed if r not in renamed]
    added = [a for a in added if a not in renamed.values()]

    for r in removed:
        changes.append({"kind": "removed", "column": r})
    for a in added:
        changes.append({"kind": "added", "column": a, "samples": ncols[a].get("samples", [])[:3]})

    common = [c for c in oorder if c in ncols or c in renamed]
    new_pos = [norder.index(renamed.get(c, c)) for c in common]
    if new_pos != sorted(new_pos):
        changes.append({"kind": "reordered"})
    for c in common:
        before, after = ocols[c], ncols[renamed.get(c, c)]
        if _shape(before) != _shape(after):
            changes.append(
                {"kind": "format", "column": renamed.get(c, c), "old": _describe(before), "new": _describe(after)}
            )
        u_old = (before.get("unit"), (before.get("number") or {}).get("unit"))
        u_new = (after.get("unit"), (after.get("number") or {}).get("unit"))
        if any(_physical(a, b) for a, b in zip(u_old, u_new, strict=True)):
            changes.append(
                {"kind": "unit", "column": renamed.get(c, c), "old": u_old[0] or u_old[1], "new": u_new[0] or u_new[1]}
            )
    return changes


PHYSICAL = {"f", "c", "°f", "°c", "mv", "v", "cents", "%", "kg", "g", "m", "cm", "mm", "l", "ml", "kwh"}


def _physical(a: str | None, b: str | None) -> bool:
    """A unit change that alters values (Fahrenheit to Celsius, millivolts to volts). A currency code appearing in
    a header ("Amount (NZD)") only labels the column and is not treated as a unit change."""
    if (a or "").lower() == (b or "").lower():
        return False
    return (a or "").lower() in PHYSICAL or (b or "").lower() in PHYSICAL


def _describe(c: dict[str, Any]) -> str:
    n = c.get("number") or {}
    bits = [c.get("kind", "?")]
    if c.get("format"):
        bits.append(str(c["format"]))
    if n.get("decimal") == ",":
        bits.append("decimal comma")
    if n.get("symbol"):
        bits.append(f"symbol {n['symbol']}")
    if n.get("unit"):
        bits.append(f"unit {n['unit']}")
    return ", ".join(bits) + f" e.g. {', '.join(map(str, c.get('samples', [])[:2]))}"


def kinds(changes: list[dict[str, Any]]) -> list[str]:
    return sorted({c["kind"] for c in changes})


def rules_update(old: Mapping, changes: list[dict[str, Any]], prof: Profile, contract: Contract) -> Mapping:
    """Apply the detected changes to the approved mapping without a model."""
    m = copy.deepcopy(old)
    m.author = "rules"
    m.review = []
    renamed = {c["old"]: c["new"] for c in changes if c["kind"] == "renamed"}
    removed = {c["column"] for c in changes if c["kind"] == "removed"}
    touched = {c.get("column") for c in changes if c["kind"] in ("format", "unit")} | set(renamed.values())
    keep: list[ColumnMap] = []
    for cm in m.columns:
        cm.source = [renamed.get(s, s) for s in cm.source]
        gone = [s for s in cm.source if s in removed or any(s == c.get("old") for c in changes if c["kind"] == "split")]
        if gone:
            m.review.append(f"{cm.target}: source column {', '.join(gone)} is no longer in the file")
            continue
        if len(cm.source) == 1 and cm.source[0] in touched and cm.transform not in ("const",):
            tr, params, note = conversion(contract.col(cm.target), prof.col(cm.source[0]))
            if (tr, params) != (cm.transform, cm.params):
                m.review.append(f"{cm.target}: conversion changed from {cm.transform} {cm.params} to {tr} {params}")
            cm.transform, cm.params, cm.note = tr, params, note or cm.note
        keep.append(cm)
    m.columns = keep
    mapped = {c.target for c in keep}
    used = m.sources()
    for c in changes:
        if c["kind"] == "split":
            for col in contract.columns:
                if col.name not in mapped:
                    d = _derive(col, prof, used)
                    if d:
                        keep.append(d)
                        mapped.add(col.name)
                        used.update(d.source)
                        m.review.append(f"{col.name}: now built from {' + '.join(d.source)}")
    m.ignore = {renamed.get(k, k): v for k, v in m.ignore.items() if k not in removed}
    for c in changes:
        if c["kind"] == "added" and c["column"] not in used:
            m.ignore[c["column"]] = "new column with no place in the table"
            m.review.append(f"new column '{c['column']}' is not loaded; confirm, or add it to the table")
        if c["kind"] == "split":
            for part in c["new"]:
                if part not in used:
                    m.ignore[part] = "part of a split column, not needed"
    order = {n: i for i, n in enumerate(contract.names)}
    m.columns.sort(key=lambda x: order.get(x.target, 99))
    return m


def needs_person(changes: list[dict[str, Any]]) -> bool:
    """Drift that can change meaning waits for a person even when every test passes.

    A new or missing column, a split column or a changed unit can all pass the tests and still be wrong (a
    Celsius reading run through the old Fahrenheit conversion is still a plausible temperature). Renames, value
    formats, delimiters and column order are safe to apply automatically once the tests pass.
    """
    return any(c["kind"] in ("added", "removed", "split", "unit") for c in changes)
