from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentic_etl import drift
from agentic_etl.agent import _code, _json
from agentic_etl.baseline import load as baseline_load
from agentic_etl.cli import main
from agentic_etl.contracts import Contract
from agentic_etl.evaluate import run as run_eval
from agentic_etl.pipeline import onboard, prepare
from agentic_etl.service import Context, do_drift, do_map, do_run
from agentic_etl.store import Workspace, default_source
from agentic_etl.web.server import App, make_handler
from tests.conftest import path


@pytest.fixture()
def ctx(tmp_path: Path) -> Context:
    return Context(ws=Workspace(tmp_path / "ws"), engine="rules")


def test_default_source_names() -> None:
    assert default_source(Path("harbour_bank_2026-03.csv")) == "harbour_bank"
    assert default_source(Path("timesheet_week12.xlsx")) == "timesheet"
    assert default_source(Path("pos_export_2026-03-02.json")) == "pos_export"


def test_run_loads_quarantines_audits_and_is_idempotent(ctx: Context) -> None:
    o, run = do_run(ctx, path("files/harbour_bank_2026-03.csv"), "harbour_bank", yes=True)
    assert o is not None and run["status"] == "loaded" and run["rows_loaded"] == 28
    o, run = do_run(ctx, path("files/harbour_bank_2026-04.csv"), "harbour_bank")
    assert run["rows_loaded"] == 26 and run["rows_quarantined"] == 3
    do_run(ctx, path("files/harbour_bank_2026-04.csv"), "harbour_bank")  # same file again
    _, rows = ctx.ws.query("SELECT count(*) FROM bank_transactions")
    assert rows[0][0] == 28 + 26
    _, q = ctx.ws.query("SELECT reasons FROM etl.quarantine ORDER BY file_row")
    assert len(q) == 3 and any("unique key" in r[0] for r in q)
    _, audit = ctx.ws.query("SELECT file_sha256, mapping_version, code_sha, rows_in FROM etl.runs")
    assert len(audit) == 3 and all(len(a[0]) == 64 and a[1] == 1 and a[2] and a[3] for a in audit)
    code_files = list((ctx.ws.source_dir("harbour_bank") / "code").glob("*.py"))
    assert code_files


def test_run_refuses_a_drifted_file(ctx: Context) -> None:
    do_run(ctx, path("files/harbour_bank_2026-03.csv"), "harbour_bank", yes=True)
    o, info = do_run(ctx, path("drift/d01_harbour_bank_2026-05.csv"), "harbour_bank")
    assert o is None and info["status"] == "drift"
    assert [c["kind"] for c in info["drift"]] == ["renamed"]


def test_drift_renamed_loads_unit_change_waits_removed_key_blocks(ctx: Context) -> None:
    do_run(ctx, path("files/harbour_bank_2026-03.csv"), "harbour_bank", yes=True)
    o, run = do_drift(ctx, path("drift/d01_harbour_bank_2026-05.csv"), "harbour_bank")
    assert o is not None and run["status"] == "loaded" and run["mapping_version"] == 2

    do_run(ctx, path("files/coldstore_fahrenheit.csv"), "coldstore", yes=True)
    o, run = do_drift(ctx, path("drift/d06_coldstore_celsius.csv"), "coldstore")
    assert run["status"] == "review" and run["rows_loaded"] == 0
    rec = ctx.ws.record("coldstore")
    assert rec is not None and rec["status"] == "proposed"
    temp = next(c for c in rec["mapping"]["columns"] if c["target"] == "temperature_c")
    assert temp["transform"] == "number"  # no longer converted from Fahrenheit

    do_run(ctx, path("files/crm_semicolon.csv"), "crm", yes=True)
    o, run = do_drift(ctx, path("drift/d09_crm_semicolon_no_id.csv"), "crm")
    assert run["status"] == "blocked" and run["rows_loaded"] == 0


def test_drift_detects_every_scenario_kind(
    manifest: dict[str, Any], files: dict[str, Any], cat: dict[str, Contract]
) -> None:
    for d in manifest["drift"]:
        _, base = prepare(path(files[d["base"]]["file"]))
        _, new = prepare(path(d["file"]))
        assert set(d["kinds"]) <= set(drift.kinds(drift.detect(drift.fingerprint(base), new))), d["id"]


def test_no_drift_against_itself(files: dict[str, Any]) -> None:
    _, prof = prepare(path(files["b2"]["file"]))
    assert drift.detect(drift.fingerprint(prof), prof) == []


def test_dbt_files(ctx: Context, tmp_path: Path) -> None:
    do_run(ctx, path("files/timesheet_week12.xlsx"), "timesheet", yes=True)
    code = main(["--home", str(ctx.ws.root), "dbt", "timesheet", "--out", str(tmp_path / "dbt")])
    assert code == 0
    schema = yaml.safe_load((tmp_path / "dbt/models/staging/stg_timesheet.yml").read_text(encoding="utf-8"))
    cols = {c["name"]: c for c in schema["models"][0]["columns"]}
    assert "not_null" in cols["employee_id"]["data_tests"]
    assert (tmp_path / "dbt/tests/stg_timesheet__key_unique.sql").exists()
    sql = (tmp_path / "dbt/models/staging/stg_timesheet.sql").read_text(encoding="utf-8")
    assert "{{ source('raw', 'timesheet') }}" in sql


def test_review_page_api(ctx: Context) -> None:
    _, v = do_map(ctx, path("files/bayline_prices.csv"), "bayline")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(ctx.ws)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        sources = json.loads(urllib.request.urlopen(base + "/api/sources").read())
        assert sources[0]["source"] == "bayline" and sources[0]["latest"]["status"] == "proposed"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "mapping review" in page

        def post(p: str, body: Any) -> dict[str, Any]:
            req = urllib.request.Request(
                base + p, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
            )
            return dict(json.loads(urllib.request.urlopen(req).read()))

        tested = post(f"/api/source/bayline/test/{v}", {})
        assert tested["status"] == "loaded" and tested["rows_loaded"] == 24
        rec = ctx.ws.record("bayline")
        assert rec is not None
        edited = post(f"/api/source/bayline/edit/{v}", rec["mapping"])
        assert edited["version"] == v + 1
        assert post(f"/api/source/bayline/approve/{v + 1}", {})["approved"] == v + 1
        rec2 = ctx.ws.record("bayline")
        assert rec2 is not None and rec2["status"] == "approved" and rec2["mapping"]["author"] == "person"
    finally:
        server.shutdown()
        server.server_close()


def test_model_reply_parsing() -> None:
    assert _json('Here you go: {"table": "x", "columns": []} thanks') == {"table": "x", "columns": []}
    assert _code("```python\nimport polars as pl\n```") == "import polars as pl\n"
    with pytest.raises(ValueError):
        _json("no json here")


def test_baseline_is_naive(files: dict[str, Any], cat: dict[str, Contract]) -> None:
    with pytest.raises(UnicodeDecodeError):
        baseline_load(path(files["b3"]["file"]), cat["bank_transactions"])
    out = baseline_load(path(files["s6"]["file"]), cat["supplier_prices"])
    assert out.height == 25  # the duplicate and the bad rows load as if they were fine


def test_rules_and_baseline_evaluation_numbers() -> None:
    r = run_eval(with_model=False, progress=lambda m: None)
    rules, base = r["summary"]["rules"], r["summary"]["baseline"]
    assert rules["exact"] == 26 and rules["planted_caught"] == 18 and rules["false_quarantines"] == 0
    assert base["exact"] == 0 and base["planted_caught"] == 0
    assert r["drift_summary"]["rules"]["handled"] == 9
    assert r["summary"]["baseline"]["silent_loss_files"] == 10


def test_offline_onboarding_without_a_key(files: dict[str, Any], cat: dict[str, Contract]) -> None:
    o = onboard(path(files["c3"]["file"]), cat, "rules")
    assert o.status == "loaded" and not o.replies


def test_promoted_code_is_reused_for_the_next_file(ctx: Context) -> None:
    do_run(ctx, path("files/harbour_bank_2026-03.csv"), "harbour_bank", yes=True)
    rec = ctx.ws.record("harbour_bank")
    assert rec is not None and rec.get("promoted_code")
    o, run = do_run(ctx, path("files/harbour_bank_2026-04.csv"), "harbour_bank")
    assert o is not None and any("re-used the promoted code" in line for line in o.log)
    assert run["code_sha"] == rec["promoted_code"]
