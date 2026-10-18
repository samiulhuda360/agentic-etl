from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from agentic_etl import checks, sandbox
from agentic_etl.compile import polars_code, sql_select
from agentic_etl.contracts import Contract
from agentic_etl.gold import compare, read_gold
from agentic_etl.mapping import problems, propose_new_table, route, rules_mapping
from agentic_etl.pipeline import onboard, prepare
from tests.conftest import path


def test_routing_picks_the_right_table(manifest: dict[str, Any], cat: dict[str, Contract]) -> None:
    right = 0
    for f in manifest["files"]:
        _, prof = prepare(path(f["file"]))
        best, _ = route(prof, cat)
        right += best == f["table"]
    assert right >= 33  # the rules router misses only files whose headers share no words with the aliases


def test_unknown_file_gets_a_new_table_proposal(cat: dict[str, Contract]) -> None:
    _, prof = prepare(path("extra/fleet_fuel_cards.csv"))
    best, _ = route(prof, cat)
    assert best is None
    contract, m = propose_new_table(prof, "new_fleet_fuel_cards")
    assert "litres" in contract.names
    assert contract.col("litres").type == "float"
    assert not problems(m, contract, prof.names)


def test_problems_catch_dropped_columns_and_missing_targets(cat: dict[str, Contract]) -> None:
    _, prof = prepare(path("files/harbour_bank_2026-03.csv"))
    m, _ = rules_mapping(prof, cat["bank_transactions"])
    assert not problems(m, cat["bank_transactions"], prof.names)
    m.columns = [c for c in m.columns if c.target != "amount"]
    m.ignore.pop("Amount", None)
    issues = problems(m, cat["bank_transactions"], prof.names)
    assert any("required column 'amount'" in i for i in issues)
    assert any("neither loaded nor ignored: Amount" in i for i in issues)


@pytest.mark.parametrize("fid", ["b1", "b2", "b3", "b5", "s1", "p1", "t2", "t4", "i2", "i5", "c2", "c5"])
def test_rules_engine_loads_exactly(fid: str, files: dict[str, Any], cat: dict[str, Contract]) -> None:
    f = files[fid]
    o = onboard(path(f["file"]), cat, "rules")
    assert o.status == "loaded", o.log
    loaded, quarantined = o.split()
    assert quarantined is not None and quarantined.height == 0
    assert compare(loaded, read_gold(path(f["gold"])), cat[f["table"]]).exact


@pytest.mark.parametrize("fid", ["b6", "s6", "p6", "t6", "i6", "c6"])
def test_planted_problems_are_quarantined_not_loaded(fid: str, files: dict[str, Any], cat: dict[str, Contract]) -> None:
    f = files[fid]
    o = onboard(path(f["file"]), cat, "rules")
    loaded, quarantined = o.split()
    assert o.status == "loaded"
    assert quarantined is not None and quarantined.height == f["quarantine"]
    assert compare(loaded, read_gold(path(f["gold"])), cat[f["table"]]).exact
    failing = {t.name.split(":")[0] for t in o.final.report.tests if not t.passed}  # type: ignore[union-attr]
    wanted = {"unique": "unique key", "not_null": "not null", "range": "in range", "accepted_values": "allowed values"}
    assert {wanted[p["test"]] for p in f["planted"]} <= failing


def _bad(code: str, old: str, new: str) -> str:
    assert old in code
    return code.replace(old, new)


def test_fidelity_tests_catch_wrong_code(cat: dict[str, Contract]) -> None:
    raw, prof = prepare(path("files/harbour_bank_export_eu.csv"))
    contract = cat["bank_transactions"]
    m, _ = rules_mapping(prof, contract)
    code = polars_code(m, contract)

    # Wrong date format: every date becomes null -> "no values lost" fails.
    wrong_date = _bad(code, '"%d.%m.%Y"', '"%m/%d/%Y"')
    rep = checks.build_and_run(sandbox.run(wrong_date, raw.df).df, raw.df, contract, m, prof)
    assert not rep.code_ok and any(t.name == "no values lost: txn_date" for t in rep.fidelity_failures)

    # Decimal comma ignored: 1.234,56 read as 1.23456 -> the totals no longer reconcile.
    no_comma = _bad(code, 'number("Amount", decimal=",", thousands=".")', 'number("Amount")')
    rep = checks.build_and_run(sandbox.run(no_comma, raw.df).df, raw.df, contract, m, prof)
    assert any(t.name == "total reconciles: amount" for t in rep.fidelity_failures)

    # Dropping rows is never allowed.
    dropper = code.replace("    return raw.select(", "    raw = raw.head(5)\n    return raw.select(")
    rep = checks.build_and_run(sandbox.run(dropper, raw.df).df, raw.df, contract, m, prof)
    assert any(t.name == "row count" for t in rep.fidelity_failures)


def test_systematic_quality_failures_count_as_code_failures(cat: dict[str, Contract]) -> None:
    raw, prof = prepare(path("files/bayline_prices.csv"))
    contract = cat["supplier_prices"]
    m, _ = rules_mapping(prof, contract)
    no_upper = _bad(polars_code(m, contract), 'text("UOM", case="upper")', 'text("UOM")')
    rep = checks.build_and_run(sandbox.run(no_upper, raw.df).df, raw.df, contract, m, prof)
    assert [t.name for t in rep.systematic] == ["allowed values: unit"]
    assert not rep.code_ok


def test_sandbox_rejects_unsafe_code_and_reports_errors() -> None:
    df = pl.DataFrame({"a": ["1"]})
    assert "import of 'os'" in (sandbox.run("import os\ndef transform(raw):\n    return raw\n", df).error or "")
    assert "'open' is not allowed" in (
        sandbox.run("def transform(raw):\n    open('x')\n    return raw\n", df).error or ""
    )
    assert "'.write_csv'" in (
        sandbox.run("def transform(raw):\n    raw.write_csv('x')\n    return raw\n", df).error or ""
    )
    res = sandbox.run("import polars as pl\ndef transform(raw):\n    return raw.select(pl.col('nope'))\n", df)
    assert not res.ok and "ColumnNotFoundError" in (res.error or "") and "line 3" in (res.error or "")
    ok = sandbox.run("import polars as pl\ndef transform(raw):\n    return raw.with_columns(b=pl.lit(2))\n", df)
    assert ok.ok and ok.df is not None and ok.df.columns == ["a", "b"]


@pytest.mark.parametrize("fid", ["b2", "b3", "s2", "p3", "t1", "t2", "i1", "i5", "c3", "c2"])
def test_dbt_sql_matches_the_polars_transform(fid: str, files: dict[str, Any], cat: dict[str, Contract]) -> None:
    f = files[fid]
    o = onboard(path(f["file"]), cat, "rules")
    assert o.mapping is not None and o.contract is not None and o.final is not None and o.final.out is not None
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "raw.parquet"
        o.raw.df.write_parquet(p)
        con.execute(f"CREATE TABLE r AS SELECT * FROM read_parquet('{p.as_posix()}')")
        cur = con.execute(sql_select(o.mapping, o.contract, "r"))
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    sql_df = pl.DataFrame(rows, infer_schema_length=None)
    assert compare(sql_df, read_gold(path(f["gold"])), cat[f["table"]]).exact


def test_required_column_set_to_a_constant_null_is_blocked(cat: dict[str, Contract]) -> None:
    from agentic_etl.mapping import ColumnMap

    _, prof = prepare(path("files/crm_semicolon.csv"))
    m, _ = rules_mapping(prof, cat["crm_contacts"])
    m.columns = [c for c in m.columns if c.target != "contact_id"] + [
        ColumnMap("contact_id", [], "const", {"value": None})
    ]
    m.ignore["ID"] = "dropped"
    assert any("required column 'contact_id'" in i for i in problems(m, cat["crm_contacts"], prof.names))


@pytest.mark.parametrize("fid", ["b2", "s4", "p4", "i4", "c4"])
def test_recorded_model_run_replays_offline(fid: str, files: dict[str, Any], cat: dict[str, Contract]) -> None:
    from agentic_etl.llm import LLM

    f = files[fid]
    llm = LLM(offline=True)
    o = onboard(path(f["file"]), cat, "model", llm)
    assert o.status == "loaded" and llm.live_calls == 0 and llm.cached_calls >= 2
    loaded, _ = o.split()
    assert compare(loaded, read_gold(path(f["gold"])), cat[f["table"]]).exact
