"""Onboard one file: profile, map, generate code, test it in the sandbox, repair, and decide what may load.

The loop in `build()` is the heart of the project:

    code = compile(mapping)  or  model.write_code(mapping)
    repeat up to N times:
        run code in the sandbox -> run the data tests
        if the code is right (fidelity tests pass): stop
        model.repair(code, failing tests)
    only a version whose fidelity tests all pass is promoted

Rows that break quality tests (duplicate keys, missing keys, out-of-range values) are split off to quarantine
with their reasons; they never block the clean rows and are never "repaired" away by the model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from . import agent, sandbox
from .checks import Report, build_and_run
from .compile import polars_code
from .contracts import Contract, from_dict
from .llm import LLM, Reply
from .mapping import Mapping, problems, propose_new_table, route, rules_mapping
from .profile import Profile, profile
from .readers import RawTable, read

ENGINES = ("rules", "model")


@dataclass
class Attempt:
    code: str
    report: Report
    out: pl.DataFrame | None
    seconds: float


@dataclass
class Outcome:
    file: str
    raw: RawTable
    prof: Profile
    engine: str
    status: str = "pending"  # loaded | failed | blocked | review
    table: str | None = None
    contract: Contract | None = None
    mapping: Mapping | None = None
    attempts: list[Attempt] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    route_scores: dict[str, float] = field(default_factory=dict)

    @property
    def final(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def first(self) -> Attempt | None:
        return self.attempts[0] if self.attempts else None

    @property
    def repairs(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def code_sha(self) -> str | None:
        return hashlib.sha256(self.final.code.encode()).hexdigest()[:12] if self.final else None

    def split(self) -> tuple[pl.DataFrame | None, pl.DataFrame | None]:
        """(rows to load, rows to quarantine with a 'reasons' column), for a promoted version."""
        a = self.final
        if a is None or a.out is None or not a.report.code_ok:
            return None, None
        q = a.report.quarantine()
        keep = [i for i in range(a.out.height) if i not in q]
        bad = sorted(q)
        quarantined = a.out[bad].with_columns(
            pl.Series("row", [r + 1 for r in bad], dtype=pl.Int64),
            pl.Series("reasons", ["; ".join(q[r]) for r in bad], dtype=pl.String),
        )
        return a.out[keep], quarantined

    def usage(self) -> dict[str, Any]:
        return {
            "calls": len(self.replies),
            "cached": sum(r.cached for r in self.replies),
            "prompt_tokens": sum(r.prompt_tokens for r in self.replies),
            "completion_tokens": sum(r.completion_tokens for r in self.replies),
            "cost_usd": round(sum(r.cost_usd for r in self.replies), 6),
        }


def prepare(path: Path) -> tuple[RawTable, Profile]:
    raw = read(path)
    return raw, profile(raw)


def test(code: str, raw: RawTable, contract: Contract, m: Mapping, prof: Profile) -> Attempt:
    res = sandbox.run(code, raw.df)
    rep = build_and_run(res.df, raw.df, contract, m, prof, res.error)
    return Attempt(code, rep, res.df, res.seconds)


def choose_mapping(o: Outcome, catalogue: dict[str, Contract], llm: LLM | None, table: str | None = None) -> None:
    """Fill o.mapping / o.contract, or set o.status = 'blocked' with the reason in the log."""
    if o.engine == "rules" or llm is None:
        if table is None:
            best, o.route_scores = route(o.prof, catalogue)
            table = best
        if table is None:
            name = "new_" + Path(o.file).stem.lower().replace("-", "_").replace(" ", "_")[:40]
            contract, m = propose_new_table(o.prof, name)
            o.log.append(
                f"no existing table fits (best score {max(o.route_scores.values(), default=0):.2f}); "
                f"proposed new table {name}"
            )
        else:
            found = catalogue.get(table)
            if found is None:
                o.status = "blocked"
                o.log.append(f"unknown table {table}")
                return
            contract = found
            m, score = rules_mapping(o.prof, contract)
            o.log.append(f"rules mapper chose {table} (fit {score:.2f})")
    else:
        tables = [catalogue[table]] if table else list(catalogue.values())
        m_or_none, reply, err = agent.propose_mapping(llm, o.prof, tables)
        o.replies.append(reply)
        if m_or_none is None:
            o.status = "failed"
            o.log.append(err or "the model returned no mapping")
            return
        m = m_or_none
        if m.table == "new" or m.table not in catalogue:
            name = "new_" + Path(o.file).stem.lower().replace("-", "_")[:40]
            contract, m2 = propose_new_table(o.prof, name)
            m2.author = "rules"
            m = m2
            o.log.append(f"model found no fitting table; proposed new table {name}")
        else:
            contract = catalogue[m.table]
            o.log.append(f"model chose {m.table}")
            issues = problems(m, contract, o.prof.names)
            if issues:
                o.log.append("mapping problems: " + "; ".join(issues))
                fixed, reply = agent.fix_mapping(llm, o.prof, contract, m, issues)
                o.replies.append(reply)
                if fixed is not None:
                    m = fixed
    o.table, o.contract, o.mapping = contract.table, contract, m


def build(o: Outcome, llm: LLM | None, max_rounds: int = 3, code_engine: str | None = None) -> None:
    """Generate the transform for o.mapping, test it, repair it; set o.status."""
    assert o.mapping is not None and o.contract is not None
    issues = problems(o.mapping, o.contract, o.prof.names)
    if issues:
        o.status = "blocked"
        o.log.append("mapping cannot be built: " + "; ".join(issues))
        return
    engine = code_engine or o.engine
    if engine == "rules" or llm is None:
        code = polars_code(o.mapping, o.contract, f"Load {o.file} into {o.contract.table}")
    else:
        code, reply = agent.write_code(llm, o.mapping, o.contract, o.prof, o.raw.df)
        o.replies.append(reply)
    att = test(code, o.raw, o.contract, o.mapping, o.prof)
    o.attempts.append(att)
    rounds = 0
    while not att.report.code_ok and engine == "model" and llm is not None and rounds < max_rounds:
        rounds += 1
        o.log.append(
            f"round {rounds}: {len(att.report.fidelity_failures) + len(att.report.systematic)} "
            "failing test(s) sent back for repair"
        )
        code, reply = agent.repair(llm, att.code, att.report, o.mapping, o.contract, o.raw.df)
        o.replies.append(reply)
        att = test(code, o.raw, o.contract, o.mapping, o.prof)
        o.attempts.append(att)
    if att.report.code_ok:
        o.status = "loaded"
        q = len(att.report.quarantine())
        o.log.append(
            f"tests pass after {o.repairs} repair round(s); {att.out.height - q if att.out is not None else 0}"
            f" rows to load, {q} to quarantine"
        )
    else:
        o.status = "failed"
        o.log.append("no version passed the fidelity tests; nothing is loaded")


def onboard(
    path: Path,
    catalogue: dict[str, Contract],
    engine: str = "rules",
    llm: LLM | None = None,
    table: str | None = None,
    mapping: Mapping | None = None,
    max_rounds: int = 3,
    code_engine: str | None = None,
) -> Outcome:
    raw, prof = prepare(path)
    o = Outcome(Path(path).name, raw, prof, engine)
    if mapping is not None:
        contract = catalogue.get(mapping.table) or (
            from_dict(mapping.new_table, new=True) if mapping.new_table else None
        )
        if contract is None:
            o.status = "blocked"
            o.log.append(f"unknown table {mapping.table}")
            return o
        o.table, o.contract, o.mapping = contract.table, contract, mapping
    else:
        choose_mapping(o, catalogue, llm, table)
    if o.status in ("blocked", "failed"):
        return o
    build(o, llm, max_rounds, code_engine)
    return o
