"""Command line: etl profile | map | approve | generate | run | drift | dbt | serve | eval | demo."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import drift as driftmod
from .checks import Report
from .llm import LLM, NoModel, OverBudget, RateLimited
from .pipeline import Outcome, prepare
from .service import Context, do_drift, do_generate, do_map, do_run
from .store import Workspace, default_source


def _ctx(args: argparse.Namespace) -> Context:
    engine = args.engine
    if engine == "auto":
        engine = "model" if os.getenv("AI_API_KEY") else "rules"
    llm = LLM(offline=getattr(args, "offline", False)) if engine == "model" else None
    return Context(
        ws=Workspace(Path(args.home)) if args.home else Workspace(), engine=engine, llm=llm, max_rounds=args.rounds
    )


def _print_mapping(o: Outcome) -> None:
    m = o.mapping
    if m is None:
        return
    print(f"\n  table: {m.table}{' (new table proposed)' if m.new_table else ''}   author: {m.author}")
    print(f"  {'target column':18} {'from':34} {'conversion':22} conf  note")
    for c in m.columns:
        params = ",".join(f"{k}={v}" for k, v in c.params.items())
        conv = f"{c.transform}{'(' + params + ')' if params else ''}"
        print(f"  {c.target:18} {' + '.join(c.source) or '-':34.34} {conv:22.22} {c.confidence:4.2f}  {c.note}")
    for k, v in m.ignore.items():
        print(f"  {'(not loaded)':18} {k:34.34} {v}")
    for r in m.review:
        print(f"  review: {r}")


def _print_tests(rep: Report | None) -> None:
    if rep is None:
        return
    for t in rep.tests:
        mark = "PASS" if t.passed else ("FAIL" if t.family == "fidelity" or t in rep.systematic else "QUAR")
        print(f"  {mark}  {t.family:8} {t.name}{'  - ' + t.detail if t.detail else ''}")


def _print_outcome(o: Outcome, run: dict[str, Any] | None = None) -> None:
    for line in o.log:
        print(f"  - {line}")
    if o.final:
        print(f"\n  tests ({len(o.attempts)} version(s) tried, {o.repairs} repair round(s)):")
        _print_tests(o.final.report)
    if run:
        print(
            f"\n  run {run['run_id']}: {run['status']}, {run['rows_loaded']} rows loaded, "
            f"{run['rows_quarantined']} quarantined, {run['lines_skipped']} non-data lines recorded, "
            f"{run['model_calls']} model call(s)"
        )


def cmd_profile(args: argparse.Namespace) -> int:
    raw, prof = prepare(Path(args.file))
    if args.json:
        print(json.dumps(prof.brief(), indent=2, ensure_ascii=False))
        return 0
    print(f"{prof.file}: {prof.rows} rows, read as {json.dumps(raw.spec.to_dict())}")
    for s in prof.skipped:
        print(f"  non-data line kept aside: {s[:90]}")
    print(f"\n  {'column':28} {'kind':14} {'format':22} nulls distinct  samples")
    for c in prof.columns:
        fmt = c.format or (
            f"decimal '{c.number.decimal}'"
            + (f" {c.number.symbol}" if c.number.symbol else "")
            + (f" {c.number.unit}" if c.number.unit else "")
            if c.number
            else ""
        )
        unit = f" [{c.unit}]" if c.unit else ""
        print(
            f"  {c.name[:28]:28} {c.kind + unit:14} {fmt[:22]:22} {c.nulls:5} {c.distinct:8}  "
            f"{', '.join(c.samples[:3])[:60]}"
        )
    print(f"\n  candidate keys: {', '.join(' + '.join(k) for k in prof.keys[:3]) or 'none'}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    o, v = do_map(ctx, Path(args.file), args.source, args.table)
    _print_mapping(o)
    print(
        f"\n  saved as mapping v{v} of source '{args.source or default_source(Path(args.file))}' (proposed). "
        "Review it with `etl serve`, or approve with `etl approve`."
    )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    ws = Workspace(Path(args.home)) if args.home else Workspace()
    rec = ws.record(args.source, args.version)
    if rec is None:
        print(f"no mapping for source '{args.source}'")
        return 1
    ws.update_mapping(args.source, rec["version"], status="approved")
    print(f"approved mapping v{rec['version']} of '{args.source}'")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    o, v = do_generate(ctx, Path(args.file), args.source, args.yes)
    print(f"mapping v{v}, engine {ctx.engine}")
    _print_outcome(o)
    if o.final and args.show_code:
        print("\n" + o.final.code)
    return 0 if o.status == "loaded" else 1


def cmd_run(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    o, run = do_run(ctx, Path(args.file), args.source, args.yes)
    if o is None:
        print(run["message"])
        for c in run["drift"]:
            print(f"  drift: {json.dumps(c, ensure_ascii=False)}")
        return 2
    _print_outcome(o, run)
    return 0 if o.status == "loaded" else 1


def cmd_drift(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    ws = ctx.ws
    rec = ws.record(args.source, approved_only=True)
    if rec is None:
        print(f"source '{args.source}' has no approved mapping")
        return 1
    _raw, prof = prepare(Path(args.file))
    changes = driftmod.detect(rec["fingerprint"], prof)
    print(f"compared with mapping v{rec['version']} (made for {rec['file']}):")
    for c in changes or [{"kind": "none"}]:
        print(f"  drift: {json.dumps(c, ensure_ascii=False)}")
    o, run = do_drift(ctx, Path(args.file), args.source, args.yes)
    if o is not None:
        _print_mapping(o)
        _print_outcome(o, run)
    return {"loaded": 0, "review": 3}.get(o.status if o else "", 1)


def cmd_dbt(args: argparse.Namespace) -> int:
    from . import dbt
    from .contracts import catalogue, from_dict

    ws = Workspace(Path(args.home)) if args.home else Workspace()
    got = ws.mapping(args.source, approved_only=True)
    if got is None:
        print(f"source '{args.source}' has no approved mapping")
        return 1
    m, _ = got
    contract = catalogue().get(m.table) or from_dict(m.new_table or {}, new=True)
    for p in dbt.write(m, contract, args.source, Path(args.out)):
        print(f"  wrote {p}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.server import serve

    serve(Workspace(Path(args.home)) if args.home else Workspace(), args.port)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evaluate

    result = evaluate.run(
        offline=args.offline, max_rounds=args.rounds, only=args.only or None, with_model=not args.no_model
    )
    print("\n" + evaluate.table(result))
    if args.check:
        want = json.loads(Path(args.check).read_text(encoding="utf-8"))
        same = want["summary"] == result["summary"] and want["drift_summary"] == result["drift_summary"]
        print(f"\nreplay {'matches' if same else 'DIFFERS FROM'} {args.check}")
        return 0 if same else 1
    path = evaluate.save(result, Path(args.out) if args.out else None)
    print(f"\nwrote {path}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import demo

    return demo(Path(args.home) if args.home else None)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="etl", description="AI-written, test-proven loads of messy files into DuckDB.")
    p.add_argument("--home", help="workspace folder (default ETL_HOME or ./.etl)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--engine",
            choices=["auto", "rules", "model"],
            default="auto",
            help="rules: no model; model: the model maps and writes code (auto picks model when AI_API_KEY is set)",
        )
        sp.add_argument("--offline", action="store_true", help="model replies from the recorded cache only")
        sp.add_argument("--rounds", type=int, default=3, help="maximum repair rounds (default 3)")

    sp = sub.add_parser("profile", help="profile a file")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_profile)

    sp = sub.add_parser("map", help="propose a mapping to a warehouse table and save it as a new version")
    sp.add_argument("file")
    sp.add_argument("--source")
    sp.add_argument("--table")
    common(sp)
    sp.set_defaults(fn=cmd_map)

    sp = sub.add_parser("approve", help="approve a mapping version")
    sp.add_argument("source")
    sp.add_argument("--version", type=int)
    sp.set_defaults(fn=cmd_approve)

    sp = sub.add_parser("generate", help="write the transform and data tests, run them in the sandbox, repair")
    sp.add_argument("file")
    sp.add_argument("--source")
    sp.add_argument("--yes", action="store_true", help="use the latest mapping even if not approved")
    sp.add_argument("--show-code", action="store_true")
    common(sp)
    sp.set_defaults(fn=cmd_generate)

    sp = sub.add_parser("run", help="test and load a file with the source's approved mapping")
    sp.add_argument("file")
    sp.add_argument("--source")
    sp.add_argument("--yes", action="store_true", help="map and approve automatically when the source is new")
    common(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("drift", help="detect layout drift against the approved mapping and propose an update")
    sp.add_argument("file")
    sp.add_argument("--source", required=True)
    sp.add_argument("--yes", action="store_true", help="approve and load when the tests pass")
    common(sp)
    sp.set_defaults(fn=cmd_drift)

    sp = sub.add_parser("dbt", help="write the approved mapping as a dbt staging model with schema tests")
    sp.add_argument("source")
    sp.add_argument("--out", default="dbt_project")
    sp.set_defaults(fn=cmd_dbt)

    sp = sub.add_parser("serve", help="the review page: mappings, test results, quarantine and runs")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("eval", help="score every approach against the gold tables")
    sp.add_argument("--offline", action="store_true", help="replay recorded model replies only")
    sp.add_argument("--no-model", action="store_true", help="baseline and rules only")
    sp.add_argument("--rounds", type=int, default=3)
    sp.add_argument("--only", nargs="*", help="file ids, e.g. b1 p4")
    sp.add_argument("--out")
    sp.add_argument("--check", help="compare the summary with a saved results file instead of writing one")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("demo", help="the Acme story end to end in a fresh workspace (no key needed)")
    sp.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    try:
        return int(args.fn(args))
    except (RateLimited, OverBudget) as e:
        print(f"stopped: {e}")
        return 4
    except NoModel as e:
        print(f"no model available: {e}. Set AI_API_KEY, or use --engine rules.")
        return 5
    except RuntimeError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
