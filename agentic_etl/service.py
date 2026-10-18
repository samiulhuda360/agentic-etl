"""The commands behind the CLI and the review page: map, generate, run and drift, with versions and audit."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agent
from . import drift as driftmod
from .contracts import Contract, catalogue
from .llm import LLM
from .mapping import Mapping
from .pipeline import Outcome, choose_mapping, onboard, prepare, test
from .store import Workspace, default_source


@dataclass
class Context:
    ws: Workspace = field(default_factory=Workspace)
    engine: str = "rules"
    llm: LLM | None = None
    max_rounds: int = 3
    tables: dict[str, Contract] = field(default_factory=catalogue)

    def with_new(self, m: Mapping) -> dict[str, Contract]:
        """The catalogue plus a proposed new table, when the mapping carries one."""
        if m.new_table:
            from .contracts import from_dict

            c = from_dict(m.new_table, new=True)
            return {**self.tables, c.table: c}
        return self.tables


def run_record(
    o: Outcome, source: str, version: int | None, drift: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    a = o.final
    loaded, quarantined = o.split()
    return {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": dt.datetime.now(dt.UTC).replace(tzinfo=None),
        "source": source,
        "file": o.file,
        "file_sha256": o.raw.sha256,
        "table_name": o.table,
        "mapping_version": version,
        "mapping_author": o.mapping.author if o.mapping else None,
        "code_sha": o.code_sha,
        "engine": o.engine,
        "status": o.status,
        "repair_rounds": o.repairs,
        "rows_in": o.raw.df.height,
        "rows_loaded": loaded.height if loaded is not None and o.status == "loaded" else 0,
        "rows_quarantined": quarantined.height if quarantined is not None and o.status == "loaded" else 0,
        "lines_skipped": len(o.raw.skipped),
        "tests_passed": a.report.summary()["passed"] if a else 0,
        "tests_failed": a.report.summary()["failed"] if a else 0,
        "model_calls": len(o.replies),
        "tests": [t.to_dict() for t in a.report.tests] if a else [],
        "drift": drift or [],
        "log": o.log + [f"skipped line: {s}" for s in o.raw.skipped],
    }


def do_map(ctx: Context, path: Path, source: str | None = None, table: str | None = None) -> tuple[Outcome, int]:
    source = source or default_source(path)
    raw, prof = prepare(path)
    o = Outcome(Path(path).name, raw, prof, ctx.engine)
    choose_mapping(o, ctx.tables, ctx.llm if ctx.engine == "model" else None, table)
    if o.mapping is None:
        raise RuntimeError("; ".join(o.log) or "no mapping could be proposed")
    version = ctx.ws.save_mapping(source, o.mapping, driftmod.fingerprint(prof), o.file, raw.sha256, path=path)
    o.log.append(f"saved mapping v{version} for source '{source}' (status: proposed)")
    return o, version


def _mapping_for(ctx: Context, source: str, yes: bool) -> tuple[Mapping, dict[str, Any]]:
    got = ctx.ws.mapping(source, approved_only=not yes)
    if got is None:
        raise RuntimeError(
            f"source '{source}' has no {'' if yes else 'approved '}mapping: run `etl map` "
            f"and approve it (or pass --yes)"
        )
    return got


def do_generate(ctx: Context, path: Path, source: str | None = None, yes: bool = False) -> tuple[Outcome, int]:
    source = source or default_source(path)
    m, rec = _mapping_for(ctx, source, yes)
    o = onboard(path, ctx.with_new(m), ctx.engine, ctx.llm, mapping=m, max_rounds=ctx.max_rounds)
    for a in o.attempts:
        ctx.ws.save_code(source, a.code)
    return o, rec["version"]


def do_run(
    ctx: Context, path: Path, source: str | None = None, yes: bool = False
) -> tuple[Outcome | None, dict[str, Any]]:
    """Load a file with the source's approved mapping. Refuses when the layout has drifted."""
    source = source or default_source(path)
    map_calls: list[Any] = []
    if ctx.ws.mapping(source, approved_only=True) is None and yes:
        mo, v = do_map(ctx, path, source)
        map_calls = mo.replies
        ctx.ws.update_mapping(source, v, status="approved")
    m, rec = _mapping_for(ctx, source, False)
    raw, prof = prepare(path)
    changes = driftmod.detect(rec["fingerprint"], prof) if rec.get("file_sha256") != raw.sha256 else []
    if [c for c in changes if c["kind"] != "reordered"]:
        return None, {
            "status": "drift",
            "source": source,
            "drift": changes,
            "message": f"the file's layout differs from mapping v{rec['version']}: run `etl drift`",
        }
    reused = _reuse(ctx, path, source, m, rec)
    if reused is not None:
        return reused, _finish(ctx, reused, source, rec["version"], changes)
    o = onboard(path, ctx.with_new(m), ctx.engine, ctx.llm, mapping=m, max_rounds=ctx.max_rounds)
    o.replies[:0] = map_calls
    return o, _finish(ctx, o, source, rec["version"], changes)


def _reuse(ctx: Context, path: Path, source: str, m: Mapping, rec: dict[str, Any]) -> Outcome | None:
    """Re-test the code already promoted for this mapping version. If it still passes, no new code is written."""
    sha = rec.get("promoted_code")
    code = ctx.ws.code(source, sha) if sha else None
    if code is None:
        return None
    raw, prof = prepare(path)
    contract = ctx.with_new(m)[m.table]
    o = Outcome(Path(path).name, raw, prof, ctx.engine, table=m.table, contract=contract, mapping=m)
    att = test(code, raw, contract, m, prof)
    if not att.report.code_ok:
        return None
    o.attempts.append(att)
    o.status = "loaded"
    q = len(att.report.quarantine())
    o.log.append(
        f"re-used the promoted code {sha} for mapping v{rec['version']}: every fidelity test passes; "
        f"{raw.df.height - q} rows to load, {q} to quarantine"
    )
    return o


def _finish(ctx: Context, o: Outcome, source: str, version: int, changes: list[dict[str, Any]]) -> dict[str, Any]:
    for a in o.attempts:
        ctx.ws.save_code(source, a.code)
    run = run_record(o, source, version, changes)
    if o.status == "loaded" and o.code_sha:
        ctx.ws.set_promoted(source, version, o.code_sha)
    if o.status == "loaded" and o.contract is not None:
        rows, quarantined = o.split()
        assert rows is not None and quarantined is not None
        ctx.ws.load(
            source=source,
            file=o.file,
            file_sha=o.raw.sha256,
            contract=o.contract,
            rows=rows,
            quarantined=quarantined,
            raw=o.raw.df,
            run=run,
        )
    ctx.ws.audit(run)
    return run


def do_drift(ctx: Context, path: Path, source: str, yes: bool = False) -> tuple[Outcome | None, dict[str, Any]]:
    """Compare with the approved mapping's fingerprint, propose an update, test it, and load only if allowed."""
    got = ctx.ws.mapping(source, approved_only=True)
    if got is None:
        raise RuntimeError(f"source '{source}' has no approved mapping to compare with")
    old, rec = got
    raw, prof = prepare(path)
    changes = driftmod.detect(rec["fingerprint"], prof)
    if not changes:
        o = onboard(path, ctx.with_new(old), ctx.engine, ctx.llm, mapping=old, max_rounds=ctx.max_rounds)
        return o, _finish(ctx, o, source, rec["version"], [])
    contract = ctx.with_new(old)[old.table]
    new: Mapping | None = None
    calls = []
    if ctx.engine == "model" and ctx.llm is not None:
        new, reply = agent.update_mapping(ctx.llm, old, changes, prof, contract)
        calls.append(reply)
    if new is None:
        new = driftmod.rules_update(old, changes, prof, contract)
    version = ctx.ws.save_mapping(
        source, new, driftmod.fingerprint(prof), Path(path).name, raw.sha256, changes, path=path
    )
    o = onboard(path, ctx.with_new(new), ctx.engine, ctx.llm, mapping=new, max_rounds=ctx.max_rounds)
    o.replies[:0] = calls
    o.log.insert(0, "drift: " + ", ".join(driftmod.kinds(changes)) + f"; proposed mapping v{version}")
    if o.status == "loaded" and driftmod.needs_person(changes) and not yes:
        o.status = "review"
        o.log.append(
            "tests pass, but this kind of change (a new, missing or split column, or a unit) needs a person: "
            "approve the mapping, then run again"
        )
    if o.status == "loaded" and not yes and not driftmod.needs_person(changes):
        o.log.append("only formats or names changed and every test passes: loaded with the updated mapping")
    if o.status == "loaded":
        ctx.ws.update_mapping(source, version, status="approved")
    return o, _finish(ctx, o, source, version, changes)
