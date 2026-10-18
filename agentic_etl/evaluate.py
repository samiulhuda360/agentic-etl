"""Evaluation against the gold tables: 36 files, 18 planted data problems, 10 drift scenarios.

Approaches compared on the same files:
    baseline        pandas readers with default settings + naive column-name matching (no tests)
    rules           profile + rules mapper (contract aliases) + compiled transform + data tests (no model)
    model+compiled  the model's mapping, compiled transform, data tests
    model first     the model's mapping and the model's transform, first version only
    model repaired  the same after up to N repair rounds driven by failing tests

Every model reply is cached under cache/llm, so `etl eval --offline` replays the exact run without a key.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from . import agent, baseline, drift
from .contracts import Contract, catalogue
from .gold import compare, read_gold
from .llm import LLM, NoModel, OverBudget, RateLimited
from .mapping import Mapping
from .paths import DATASETS, ROOT
from .pipeline import Outcome, build, onboard

PLANTED_TEST = {
    "unique": "unique key",
    "not_null": "not null",
    "range": "in range",
    "accepted_values": "allowed values",
}


@dataclass
class FileScore:
    id: str
    approach: str
    table_ok: bool
    promoted: bool
    exact: bool
    accuracy: float
    quarantined: int
    quarantine_expected: int
    planted: int
    planted_caught: int
    silent_loss: bool
    status: str
    calls: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    repairs: int = 0
    note: str = ""
    wrong: dict[str, int] = field(default_factory=dict)


def _keys(df: pl.DataFrame | None, contract: Contract) -> set[tuple[str, ...]]:
    if df is None or not all(k in df.columns for k in contract.key):
        return set()
    out = set()
    for r in df.select(contract.key).rows():
        out.add(
            tuple(
                ""
                if v is None
                else (
                    v.strftime("%Y-%m-%d %H:%M:%S")
                    if hasattr(v, "hour")
                    else v.isoformat()
                    if hasattr(v, "isoformat")
                    else str(v)
                )
                for v in r
            )
        )
    return out


def _key_text(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "hour"):
        return str(v.strftime("%Y-%m-%d %H:%M:%S"))
    if hasattr(v, "isoformat"):
        return str(v.isoformat())
    return str(v)


def silent_loss(
    loaded: pl.DataFrame | None,
    quarantined: pl.DataFrame | None,
    gold: pl.DataFrame,
    contract: Contract,
    rows_in: int | None = None,
) -> bool:
    """Data that disappeared without a record: fewer rows loaded plus quarantined than the file holds, or a loaded
    value that is empty where the gold table has one (rows matched by key)."""
    if loaded is None:
        return False
    held = loaded.height + (quarantined.height if quarantined is not None else 0)
    if rows_in is not None and held < rows_in:
        return True
    names = [c.name for c in contract.columns if c.name in loaded.columns]
    g = {tuple(r[k] or "" for k in contract.key): r for r in gold.to_dicts()}
    for r in loaded.to_dicts():
        gr = g.get(tuple(_key_text(r.get(x)) for x in contract.key))
        if gr and any((r[n] is None or r[n] == "") and gr.get(n) not in (None, "") for n in names):
            return True
    return False


def score_outcome(o: Outcome, f: dict[str, Any], approach: str, attempt: str = "final") -> FileScore:
    contract = catalogue()[f["table"]]
    gold = read_gold(DATASETS / f["gold"])
    a = o.final if attempt == "final" else o.first
    promoted = bool(a and a.out is not None and a.report.code_ok and o.table == f["table"])
    loaded = quarantined = None
    if promoted and a is not None and a.out is not None:
        q = a.report.quarantine()
        loaded = a.out[[i for i in range(a.out.height) if i not in q]]
        quarantined = a.out[sorted(q)]
    sc = compare(loaded, gold, contract)
    caught = 0
    if promoted and a is not None:
        failing = [t.name for t in a.report.tests if not t.passed and t.family == "quality"]
        for p in f["planted"]:
            if any(n.startswith(PLANTED_TEST[p["test"]]) for n in failing):
                caught += 1
    replies = o.replies if attempt == "final" else o.replies[: len(o.replies) - o.repairs]
    return FileScore(
        id=f["id"],
        approach=approach,
        table_ok=o.table == f["table"],
        promoted=promoted,
        exact=promoted and sc.exact,
        accuracy=round(sc.accuracy, 4) if promoted else 0.0,
        quarantined=quarantined.height if quarantined is not None else 0,
        quarantine_expected=f["quarantine"],
        planted=len(f["planted"]),
        planted_caught=caught,
        silent_loss=silent_loss(loaded, quarantined, gold, contract, f["rows"] + f["quarantine"])
        if promoted
        else False,
        status=o.status if attempt == "final" else ("loaded" if promoted else "failed"),
        calls=len(replies),
        cost_usd=round(sum(r.cost_usd for r in replies), 6),
        prompt_tokens=sum(r.prompt_tokens for r in replies),
        completion_tokens=sum(r.completion_tokens for r in replies),
        repairs=o.repairs if attempt == "final" else 0,
        note="; ".join(o.log[-2:])[:300],
        wrong=sc.wrong,
    )


def score_baseline(f: dict[str, Any]) -> FileScore:
    contract = catalogue()[f["table"]]
    gold = read_gold(DATASETS / f["gold"])
    try:
        out: pl.DataFrame | None = baseline.load(DATASETS / f["file"], contract)
        note = ""
    except Exception as e:  # noqa: BLE001 - a crash is the baseline's loud failure
        out, note = None, f"{type(e).__name__}: {str(e)[:120]}"
    sc = compare(out, gold, contract)
    return FileScore(
        id=f["id"],
        approach="baseline",
        table_ok=True,
        promoted=out is not None,
        exact=sc.exact,
        accuracy=round(sc.accuracy, 4),
        quarantined=0,
        quarantine_expected=f["quarantine"],
        planted=len(f["planted"]),
        planted_caught=0,
        silent_loss=silent_loss(out, None, gold, contract, f["rows"] + f["quarantine"]) if out is not None else False,
        status="loaded" if out is not None else "crashed",
        note=note,
        wrong=sc.wrong,
    )


def summarise(scores: list[FileScore]) -> dict[str, Any]:
    n = len(scores)
    clean = [s for s in scores if s.planted == 0]
    return {
        "files": n,
        "exact": sum(s.exact for s in scores),
        "exact_pct": round(100 * sum(s.exact for s in scores) / n, 1) if n else 0.0,
        "cell_accuracy_pct": round(100 * sum(s.accuracy for s in scores) / n, 1) if n else 0.0,
        "loaded": sum(s.promoted for s in scores),
        "table_ok": sum(s.table_ok for s in scores),
        "planted": sum(s.planted for s in scores),
        "planted_caught": sum(s.planted_caught for s in scores),
        "quarantine_right": sum(s.quarantined == s.quarantine_expected for s in scores if s.planted),
        "files_with_planted": sum(1 for s in scores if s.planted),
        "false_quarantines": sum(s.quarantined for s in clean),
        "silent_loss_files": sum(s.silent_loss for s in scores),
        "model_calls": sum(s.calls for s in scores),
        "calls_per_file": round(sum(s.calls for s in scores) / n, 2) if n else 0.0,
        "cost_usd_per_file": round(sum(s.cost_usd for s in scores) / n, 6) if n else 0.0,
        "tokens_per_file": round(sum(s.prompt_tokens + s.completion_tokens for s in scores) / n) if n else 0,
        "repair_rounds": sum(s.repairs for s in scores),
    }


# ---------------------------------------------------------------- drift


@dataclass
class DriftScore:
    id: str
    approach: str
    expected: list[str]
    detected: list[str]
    expect: str
    status: str
    exact: bool
    handled: bool
    silent_loss: bool
    calls: int = 0
    note: str = ""


def run_drift(
    d: dict[str, Any], base_mapping: Mapping | None, base_file: dict[str, Any], approach: str, llm: LLM | None
) -> DriftScore:
    cat = catalogue()
    contract = cat[d["table"]]
    gold = read_gold(DATASETS / d["gold"])
    if base_mapping is None:
        return DriftScore(
            d["id"],
            approach,
            d["kinds"],
            [],
            d["expect"],
            "no approved base mapping",
            False,
            False,
            False,
            note="the base file never loaded exactly, so there was no approved mapping to start",
        )
    base = onboard(DATASETS / base_file["file"], cat, "rules", mapping=base_mapping)
    from .pipeline import prepare

    _raw, prof = prepare(DATASETS / d["file"])
    fp = drift.fingerprint(base.prof)
    if approach == "no drift step":
        changes: list[dict[str, Any]] = []
        new = base_mapping
    else:
        changes = drift.detect(fp, prof)
        new = None
        calls = []
        if approach == "model" and llm is not None and changes:
            new, reply = agent.update_mapping(llm, base_mapping, changes, prof, contract)
            calls.append(reply)
        if new is None:
            new = drift.rules_update(base_mapping, changes, prof, contract) if changes else base_mapping
    engine = "model" if approach == "model" else "rules"
    o = onboard(DATASETS / d["file"], cat, engine, llm if engine == "model" else None, mapping=new)
    if approach == "model":
        o.replies[:0] = calls
    status = o.status
    if status == "loaded" and drift.needs_person(changes):
        status = "review"
    loaded, quarantined = o.split()
    sc = compare(loaded, gold, contract) if loaded is not None else None
    exact = bool(sc and sc.exact)
    detected = drift.kinds(changes)
    if approach == "no drift step":  # nothing is detected by design: judge the outcome only
        handled = status in ("blocked", "failed") if d["expect"] == "blocked" else status == "loaded" and exact
    elif d["expect"] == "blocked":
        handled = status in ("blocked", "failed") and set(d["kinds"]) <= set(detected)
    else:
        handled = status == d["expect"] and exact and set(d["kinds"]) <= set(detected)
    loss = silent_loss(loaded, quarantined, gold, contract, d["rows"]) if status == "loaded" else False
    return DriftScore(
        d["id"],
        approach,
        d["kinds"],
        detected,
        d["expect"],
        status,
        exact,
        handled,
        loss,
        len(o.replies),
        "; ".join(o.log[-2:])[:300],
    )


def drift_baseline(d: dict[str, Any]) -> DriftScore:
    contract = catalogue()[d["table"]]
    gold = read_gold(DATASETS / d["gold"])
    try:
        out: pl.DataFrame | None = baseline.load(DATASETS / d["file"], contract)
    except Exception:  # noqa: BLE001
        out = None
    sc = compare(out, gold, contract)
    status = "loaded" if out is not None else "crashed"
    handled = d["expect"] != "blocked" and sc.exact
    return DriftScore(
        d["id"],
        "baseline",
        d["kinds"],
        [],
        d["expect"],
        status,
        sc.exact,
        handled,
        silent_loss(out, None, gold, contract, d["rows"]) if out is not None else False,
    )


# ---------------------------------------------------------------- the whole evaluation


def run(
    offline: bool = False,
    max_rounds: int = 3,
    only: list[str] | None = None,
    with_model: bool = True,
    progress: Any = print,
) -> dict[str, Any]:
    manifest = json.loads((DATASETS / "manifest.json").read_text(encoding="utf-8"))
    files = [f for f in manifest["files"] if not only or f["id"] in only]
    cat = catalogue()
    llm = LLM(offline=offline) if with_model else None
    scores: dict[str, list[FileScore]] = {
        k: [] for k in ("baseline", "rules", "model+compiled", "model first", "model repaired")
    }
    base_maps: dict[str, dict[str, Mapping]] = {"rules": {}, "model": {}}
    stopped = None
    started = time.time()
    for f in files:
        path = DATASETS / f["file"]
        scores["baseline"].append(score_baseline(f))
        r = onboard(path, cat, "rules")
        s = score_outcome(r, f, "rules")
        scores["rules"].append(s)
        if s.exact and r.mapping:
            base_maps["rules"][f["id"]] = r.mapping
        msg = (
            f"{f['id']:>3}  baseline {'exact' if scores['baseline'][-1].exact else '-':5}  rules {s.status:7}"
            f" {'exact' if s.exact else '-':5}"
        )
        if llm is not None and stopped is None:
            try:
                o = onboard(path, cat, "model", llm, max_rounds=max_rounds)
            except (RateLimited, OverBudget, NoModel) as e:
                stopped = f"{type(e).__name__} at {f['id']}: {e}"
                progress(msg + f"  model stopped: {e}")
                continue
            first = score_outcome(o, f, "model first", attempt="first")
            final = score_outcome(o, f, "model repaired")
            scores["model first"].append(first)
            scores["model repaired"].append(final)
            if o.mapping is not None and o.contract is not None and o.table == f["table"]:
                c = Outcome(o.file, o.raw, o.prof, "rules", table=o.table, contract=o.contract, mapping=o.mapping)
                build(c, None)
                c.replies = o.replies[:1]
                mc = score_outcome(c, f, "model+compiled")
            else:
                mc = FileScore(
                    f["id"],
                    "model+compiled",
                    False,
                    False,
                    False,
                    0.0,
                    0,
                    f["quarantine"],
                    len(f["planted"]),
                    0,
                    False,
                    o.status,
                    note="; ".join(o.log[-2:]),
                )
            scores["model+compiled"].append(mc)
            if final.exact and o.mapping:
                base_maps["model"][f["id"]] = o.mapping
            msg += (
                f"  model first {'exact' if first.exact else '-':5} repaired {final.status:7} "
                f"{'exact' if final.exact else '-':5} rounds={o.repairs} calls={final.calls}"
            )
        progress(msg)

    drift_scores: dict[str, list[DriftScore]] = {"baseline": [], "no drift step": [], "rules": [], "model": []}
    by_id = {f["id"]: f for f in manifest["files"]}
    for d in manifest["drift"]:
        if only and d["base"] not in only:
            continue
        base = by_id[d["base"]]
        drift_scores["baseline"].append(drift_baseline(d))
        any_base = base_maps["rules"].get(d["base"]) or base_maps["model"].get(d["base"])
        drift_scores["no drift step"].append(run_drift(d, any_base, base, "no drift step", None))
        drift_scores["rules"].append(run_drift(d, base_maps["rules"].get(d["base"]), base, "rules", None))
        if llm is not None and stopped is None:
            try:
                drift_scores["model"].append(run_drift(d, base_maps["model"].get(d["base"]), base, "model", llm))
            except (RateLimited, OverBudget, NoModel) as e:
                stopped = f"{type(e).__name__} at {d['id']}: {e}"
        progress(
            f"{d['id']}  "
            + "  ".join(
                f"{k} {'ok' if v[-1].handled else '-'}" for k, v in drift_scores.items() if v and v[-1].id == d["id"]
            )
        )

    result = {
        "model": llm.model if llm else None,
        "max_repair_rounds": max_rounds,
        "live_calls_this_run": llm.live_calls if llm else 0,
        "stopped": stopped,
        "seconds": round(time.time() - started, 1),
        "summary": {k: summarise(v) for k, v in scores.items() if v},
        "drift_summary": {
            k: {
                "scenarios": len(v),
                "handled": sum(x.handled for x in v),
                "silent_loss": sum(x.silent_loss for x in v),
                "model_calls": sum(x.calls for x in v),
            }
            for k, v in drift_scores.items()
            if v
        },
        "files": {k: [vars(s) for s in v] for k, v in scores.items()},
        "drift": {k: [vars(s) for s in v] for k, v in drift_scores.items()},
    }
    return result


def save(result: dict[str, Any], path: Path | None = None) -> Path:
    path = path or ROOT / "eval" / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and "recorded" not in result:  # keep the note on when and with what the replies were recorded
        old = json.loads(path.read_text(encoding="utf-8"))
        if "recorded" in old:
            result = {"recorded": old["recorded"], **result}
    path.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return path


def table(result: dict[str, Any]) -> str:
    rows = [
        ("approach", "exact", "cells", "loaded", "planted caught", "false q", "silent loss", "calls/file", "USD/file")
    ]
    for k, s in result["summary"].items():
        rows.append(
            (
                k,
                f"{s['exact']}/{s['files']}",
                f"{s['cell_accuracy_pct']}%",
                str(s["loaded"]),
                f"{s['planted_caught']}/{s['planted']}",
                str(s["false_quarantines"]),
                str(s["silent_loss_files"]),
                str(s["calls_per_file"]),
                f"{s['cost_usd_per_file']:.5f}",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    out.append("")
    out.append(
        "drift scenarios handled: "
        + ", ".join(f"{k} {v['handled']}/{v['scenarios']}" for k, v in result["drift_summary"].items())
    )
    if result.get("stopped"):
        out.append(f"stopped early: {result['stopped']}")
    return "\n".join(out)
