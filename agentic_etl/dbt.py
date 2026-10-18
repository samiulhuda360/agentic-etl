"""Write the approved mapping as a dbt staging model, so the same load can live in a real dbt project.

Output (for source `harbour_bank` mapped to `bank_transactions`):
    models/staging/sources.yml                       the raw text tables this tool keeps in DuckDB (schema raw)
    models/staging/stg_harbour_bank.sql              the mapping compiled to DuckDB SQL
    models/staging/stg_harbour_bank.yml              the contract's data tests in dbt's own test syntax

Only built-in dbt tests are used (not_null, unique, accepted_values), plus singular tests for ranges and the key,
so the project needs no packages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .compile import sql_select
from .contracts import Contract
from .mapping import Mapping
from .store import slug


def model_sql(m: Mapping, contract: Contract, source: str) -> str:
    head = (
        f"-- Staging model for source '{source}' -> {contract.table}.\n"
        f"-- Generated from the approved mapping by agentic-etl; edit the mapping, not this file.\n"
    )
    return head + sql_select(m, contract, f"{{{{ source('raw', '{slug(source)}') }}}}")


def schema_yml(contract: Contract, source: str) -> dict[str, Any]:
    cols = []
    for c in contract.columns:
        tests: list[Any] = []
        if not c.nullable:
            tests.append("not_null")
        if c.accepted:
            tests.append({"accepted_values": {"values": c.accepted}})
        if len(contract.key) == 1 and c.name == contract.key[0]:
            tests.append("unique")
        col: dict[str, Any] = {"name": c.name, "description": c.description}
        if tests:
            col["data_tests"] = tests
        cols.append(col)
    return {
        "version": 2,
        "models": [{"name": f"stg_{slug(source)}", "description": contract.description, "columns": cols}],
    }


def singular_tests(contract: Contract, source: str) -> dict[str, str]:
    model = f"stg_{slug(source)}"
    out = {}
    for c in contract.columns:
        if not c.range:
            continue
        lo, hi = c.range
        if c.type in ("date", "datetime"):
            cond = f"{c.name} < DATE '{lo}' OR {c.name} > DATE '{hi}'"
        else:
            cond = f"{c.name} < {lo} OR {c.name} > {hi}"
        out[f"{model}__{c.name}_in_range.sql"] = f"select * from {{{{ ref('{model}') }}}}\nwhere {cond}\n"
    if len(contract.key) > 1:
        keys = ", ".join(contract.key)
        out[f"{model}__key_unique.sql"] = (
            f"select {keys}, count(*) as n from {{{{ ref('{model}') }}}}\ngroup by {keys}\nhaving count(*) > 1\n"
        )
    return out


def write(m: Mapping, contract: Contract, source: str, out_dir: Path) -> list[Path]:
    staging = out_dir / "models" / "staging"
    tests = out_dir / "tests"
    staging.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    name = f"stg_{slug(source)}"
    written = []
    p = staging / f"{name}.sql"
    p.write_text(model_sql(m, contract, source), encoding="utf-8")
    written.append(p)
    p = staging / f"{name}.yml"
    p.write_text(yaml.safe_dump(schema_yml(contract, source), sort_keys=False, allow_unicode=True), encoding="utf-8")
    written.append(p)
    src = staging / "sources.yml"
    doc: dict[str, Any] = (
        yaml.safe_load(src.read_text(encoding="utf-8"))
        if src.exists()
        else {"version": 2, "sources": [{"name": "raw", "schema": "raw", "tables": []}]}
    )
    tables = doc["sources"][0]["tables"]
    if not any(t["name"] == slug(source) for t in tables):
        tables.append({"name": slug(source), "description": f"Raw text of the latest '{source}' file."})
    src.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    written.append(src)
    for fname, sql in singular_tests(contract, source).items():
        p = tests / fname
        p.write_text(sql, encoding="utf-8")
        written.append(p)
    return written
