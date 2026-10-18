"""`etl demo`: the Acme Home & Garden story, end to end, in a fresh workspace. Needs no key.

It uses the model when replies for these files are recorded in cache/llm (they are, from the evaluation run),
and falls back to the rules engine for any step that is not recorded.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import dbt
from .llm import LLM, NoModel
from .paths import DATASETS, home
from .service import Context, do_drift, do_run
from .store import Workspace

STEPS = [
    (
        "run",
        "files/harbour_bank_2026-03.csv",
        "harbour_bank",
        "March statement from Harbour Bank: preamble lines, the account number only in the header, a footer",
    ),
    (
        "run",
        "files/harbour_bank_2026-04.csv",
        "harbour_bank",
        "April statement: same layout, but it carries a duplicate line, a line with no reference and a 2031 date",
    ),
    (
        "drift",
        "drift/d01_harbour_bank_2026-05.csv",
        "harbour_bank",
        "May statement: the bank renamed 'Amount' to 'Amount (NZD)'",
    ),
    (
        "run",
        "files/acme_fasteners_2026.xlsx",
        "acme_fasteners",
        "A supplier price list: three sheets, a notes sheet and a merged two-row header",
    ),
    ("run", "files/coldstore_fahrenheit.csv", "coldstore_f", "Cold-store sensors reporting in Fahrenheit"),
    (
        "drift",
        "drift/d06_coldstore_celsius.csv",
        "coldstore_f",
        "The sensor gateway was updated and now reports Celsius",
    ),
]


def demo(root: Path | None = None) -> int:
    root = root or home() / "demo"
    shutil.rmtree(root, ignore_errors=True)
    ws = Workspace(root)
    model = Context(ws=ws, engine="model", llm=LLM(offline=True))
    rules = Context(ws=ws, engine="rules")
    print(f"workspace: {root}\n")
    for i, (kind, rel, source, story) in enumerate(STEPS, 1):
        path = DATASETS / rel
        print(f"{i}. {story}\n   etl {kind} {rel}{' --source ' + source if kind == 'drift' else ''}")
        for ctx in (model, rules):
            try:
                o, run = do_run(ctx, path, source, yes=True) if kind == "run" else do_drift(ctx, path, source)
                break
            except NoModel:
                print("   (no recorded model reply for this step: using the rules engine)")
        if o is None:
            print(f"   {run['message']}")
            continue
        for line in o.log:
            print(f"   - {line}")
        failed = [t for t in o.final.report.tests if not t.passed] if o.final else []
        for t in failed:
            print(f"   ! {t.name}: {t.detail}")
        print(
            f"   => {run['status']}: {run['rows_loaded']} rows loaded, {run['rows_quarantined']} quarantined, "
            f"{run['model_calls']} model call(s), mapping v{run['mapping_version']} by {run['mapping_author']}\n"
        )
    got = ws.mapping("harbour_bank", approved_only=True)
    if got:
        from .contracts import catalogue

        m, _ = got
        out = root / "dbt_project"
        dbt.write(m, catalogue()[m.table], "harbour_bank", out)
        print(f"dbt staging model for harbour_bank written to {out / 'models' / 'staging'}")
    names, rows = ws.query("SELECT table_name, count(*) FROM etl.runs WHERE status = 'loaded' GROUP BY 1 ORDER BY 1")
    print("\nwarehouse:")
    for t, _n in rows:
        _, c = ws.query(f'SELECT count(*) FROM "{t}"')
        print(f"   {t:20} {c[0][0]:4} rows")
    _, q = ws.query("SELECT count(*) FROM etl.quarantine")
    _, r = ws.query("SELECT count(*) FROM etl.runs")
    print(f"   etl.quarantine       {q[0][0]:4} rows\n   etl.runs             {r[0][0]:4} audit records")
    print(f"\nreview page:  etl --home {root} serve")
    shutil.rmtree(root / "_unused", ignore_errors=True)
    return 0
