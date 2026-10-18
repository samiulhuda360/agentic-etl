"""The model's three jobs: propose a mapping, write the transform, and repair it from failing tests.

Each job is one chat call with a self-contained prompt (no hidden conversation state), so every reply can be
cached by its prompt and replayed offline. The model never sees the alias lists in the contracts: it matches
columns by meaning, from names, descriptions, types and sample values.
"""

from __future__ import annotations

import json
import re
from typing import Any

import polars as pl

from .checks import Report
from .contracts import Contract
from .llm import LLM, Reply
from .mapping import VOCABULARY, Mapping, from_dict
from .profile import Profile

SYSTEM_MAP = (
    "You are a careful data engineer. You map columns of a messy source file onto a warehouse table. "
    "You answer with one JSON object and nothing else."
)
SYSTEM_CODE = (
    f"You write small, readable Python using Polars {pl.__version__}. "
    "You answer with one ```python code block and nothing else."
)


def _json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the reply holds no JSON object")
    return dict(json.loads(m.group(0)))


def _code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


MAP_RULES = """Rules:
- Map every column of the chosen table. Omit a column only if the file truly has nothing for it and it is nullable.
- "source" lists the file column(s) a value comes from. Columns starting with "@" hold values found outside the
  table (title lines, top-level JSON fields, sheet names); they repeat on every row and may be used as sources.
- Every file column that does not start with "@" must be used as a source or listed in "ignore" with a reason.
- Take formats from the profile: the strftime "format" of dates, number.decimal / number.thousands, units.
- Convert to the table's units and conventions (for example cents to currency units, Fahrenheit to Celsius,
  millivolts to volts, upper-case codes, "First Last" names, E.164 phone numbers).
- Put anything a person should double-check in "review" (for example a date format that also fits day/month swapped).
- Use only these transforms:
"""

MAP_SHAPE = """Answer with JSON shaped like:
{"table": "<table name, or 'new' if no table fits>",
 "columns": [{"target": "<table column>", "source": ["<file column>"], "transform": "<transform>",
              "params": {}, "confidence": 0.9, "note": "<short reason>"}],
 "ignore": {"<file column>": "<why it is not loaded>"},
 "review": ["<point for the reviewer>"]}"""


def map_prompt(prof: Profile, tables: list[Contract]) -> list[dict[str, str]]:
    vocab = "\n".join(f"  {k}: {v}" for k, v in VOCABULARY.items())
    user = (
        "Warehouse tables:\n" + json.dumps([t.brief() for t in tables], ensure_ascii=False) + "\n\n"
        "Profile of the new file:\n" + json.dumps(prof.brief(), ensure_ascii=False) + "\n\n"
        "Choose the table this file belongs to and map it.\n" + MAP_RULES + vocab + "\n\n" + MAP_SHAPE
    )
    return [{"role": "system", "content": SYSTEM_MAP}, {"role": "user", "content": user}]


def propose_mapping(llm: LLM, prof: Profile, tables: list[Contract]) -> tuple[Mapping | None, Reply, str | None]:
    reply = llm.chat(map_prompt(prof, tables), json_mode=True)
    try:
        d = _json(reply.text)
        d["author"] = "model"
        return from_dict(d), reply, None
    except (ValueError, KeyError, TypeError) as e:
        return None, reply, f"unreadable mapping: {e}"


def fix_mapping_prompt(prof: Profile, contract: Contract, m: Mapping, issues: list[str]) -> list[dict[str, str]]:
    vocab = "\n".join(f"  {k}: {v}" for k, v in VOCABULARY.items())
    user = (
        "Table:\n" + json.dumps(contract.brief(), ensure_ascii=False) + "\n\n"
        "Profile of the file:\n" + json.dumps(prof.brief(), ensure_ascii=False) + "\n\n"
        "Your mapping:\n" + m.dumps() + "\n\n"
        "It has these problems:\n" + "\n".join(f"- {i}" for i in issues) + "\n\n"
        "Return the corrected mapping.\n" + MAP_RULES + vocab + "\n\n" + MAP_SHAPE
    )
    return [{"role": "system", "content": SYSTEM_MAP}, {"role": "user", "content": user}]


def fix_mapping(
    llm: LLM, prof: Profile, contract: Contract, m: Mapping, issues: list[str]
) -> tuple[Mapping | None, Reply]:
    reply = llm.chat(fix_mapping_prompt(prof, contract, m, issues), json_mode=True)
    try:
        d = _json(reply.text)
        d["author"] = "model"
        d["table"] = contract.table
        return from_dict(d), reply
    except (ValueError, KeyError, TypeError):
        return None, reply


CODE_RULES = """Rules:
- Define `def transform(raw: pl.DataFrame) -> pl.DataFrame`. Every column of `raw` is a String column.
- Return exactly the target columns, in the order given, with exactly the given Polars types.
- Keep every input row, in the same order. Never filter, drop, deduplicate, sort or invent rows or values:
  bad records are caught later by data tests and quarantined.
- Treat "", "N/A", "n/a", "-", "null" and similar placeholders as missing values (null).
- "decimal" columns are pl.Float64 rounded to 2 places; "datetime" columns are pl.Datetime("us").
- Parse leniently (strict=False) so one odd value becomes null instead of crashing the whole load.
- Import only polars (as pl), re, datetime and math. No file, network or system access.
- Keep it short and readable: one small helper per kind of conversion is fine."""


def _columns_brief(raw: pl.DataFrame) -> list[dict[str, Any]]:
    out = []
    for c in raw.columns:
        vals = [v for v in raw[c].to_list() if v not in (None, "")]
        seen: list[str] = []
        for v in vals:
            if v not in seen:
                seen.append(v)
            if len(seen) == 4:
                break
        out.append({"name": c, "examples": [s[:50] for s in seen]})
    return out


def code_prompt(m: Mapping, contract: Contract, prof: Profile, raw: pl.DataFrame) -> list[dict[str, str]]:
    target = [
        {"name": c.name, "type": c.type, "polars": str(contract.schema()[c.name]), "description": c.description}
        for c in contract.columns
    ]
    used = m.sources()
    user = (
        "Write the transform that loads this file into the warehouse table.\n\n"
        "Source columns of `raw` (all String):\n" + json.dumps(_columns_brief(raw), ensure_ascii=False) + "\n\n"
        "Profile of the source columns that are used:\n"
        + json.dumps([c.brief() for c in prof.columns if c.name in used], ensure_ascii=False)
        + "\n\n"
        "Target columns, in order:\n" + json.dumps(target, ensure_ascii=False) + "\n\n"
        "The reviewed mapping to implement:\n" + m.dumps() + "\n\n" + CODE_RULES
    )
    return [{"role": "system", "content": SYSTEM_CODE}, {"role": "user", "content": user}]


def write_code(llm: LLM, m: Mapping, contract: Contract, prof: Profile, raw: pl.DataFrame) -> tuple[str, Reply]:
    reply = llm.chat(code_prompt(m, contract, prof, raw))
    return _code(reply.text), reply


def failures_text(report: Report) -> str:
    lines = []
    for t in report.tests:
        if t.passed:
            continue
        if t.family == "quality" and t not in report.systematic:
            continue
        lines.append(f"- {t.name}: {t.detail}")
        for ex in t.examples[:3]:
            lines.append("    example: " + json.dumps(ex, ensure_ascii=False, default=str))
    return "\n".join(lines)


def repair_prompt(code: str, report: Report, m: Mapping, contract: Contract, raw: pl.DataFrame) -> list[dict[str, str]]:
    target = [{"name": c.name, "polars": str(contract.schema()[c.name])} for c in contract.columns]
    user = (
        "This transform failed its data tests. Fix the code so the tests pass.\n\n"
        "Code:\n```python\n" + code.strip() + "\n```\n\n"
        "Failed tests:\n" + failures_text(report) + "\n\n"
        "Source columns of `raw` (all String):\n" + json.dumps(_columns_brief(raw), ensure_ascii=False) + "\n\n"
        "Target columns, in order:\n" + json.dumps(target) + "\n\n"
        "The mapping it implements:\n" + m.dumps() + "\n\n" + CODE_RULES + "\n\nReturn the complete corrected code."
    )
    return [{"role": "system", "content": SYSTEM_CODE}, {"role": "user", "content": user}]


def repair(llm: LLM, code: str, report: Report, m: Mapping, contract: Contract, raw: pl.DataFrame) -> tuple[str, Reply]:
    reply = llm.chat(repair_prompt(code, report, m, contract, raw))
    return _code(reply.text), reply


def drift_prompt(
    old: Mapping, changes: list[dict[str, Any]], prof: Profile, contract: Contract
) -> list[dict[str, str]]:
    vocab = "\n".join(f"  {k}: {v}" for k, v in VOCABULARY.items())
    user = (
        "A new file arrived from a source whose approved mapping is below, and its layout has drifted.\n\n"
        "Table:\n" + json.dumps(contract.brief(), ensure_ascii=False) + "\n\n"
        "Approved mapping for the previous file:\n" + old.dumps() + "\n\n"
        "Detected changes:\n" + json.dumps(changes, ensure_ascii=False) + "\n\n"
        "Profile of the new file:\n" + json.dumps(prof.brief(), ensure_ascii=False) + "\n\n"
        "Return the updated mapping for the new file. Keep everything that still applies. If a column the table "
        'requires has no source any more, leave it out and say so in "review"; never invent values. A new file '
        'column with no place in the table goes in "ignore" and is named in "review".\n'
        + MAP_RULES
        + vocab
        + "\n\n"
        + MAP_SHAPE
    )
    return [{"role": "system", "content": SYSTEM_MAP}, {"role": "user", "content": user}]


def update_mapping(
    llm: LLM, old: Mapping, changes: list[dict[str, Any]], prof: Profile, contract: Contract
) -> tuple[Mapping | None, Reply]:
    reply = llm.chat(drift_prompt(old, changes, prof, contract), json_mode=True)
    try:
        d = _json(reply.text)
        d["author"] = "model"
        d["table"] = contract.table
        return from_dict(d), reply
    except (ValueError, KeyError, TypeError):
        return None, reply
