"""Compile a mapping into readable code: a Polars transform (what runs) and a DuckDB SQL select (the dbt model).

This is the path that needs no model: every conversion in the mapping vocabulary has one hand-written Polars
helper and one SQL expression. The model-written transform in `agent.py` has to meet the same contract and pass
the same data tests.
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import Contract
from .mapping import ColumnMap, Mapping

HELPERS = '''
NULLS = ["", "na", "n/a", "null", "none", "nil", "-", "--", "nan", "?", "#n/a"]


def clean(col: str) -> pl.Expr:
    """Trimmed text, with the usual placeholders for 'no value' turned into nulls."""
    s = pl.col(col).str.strip_chars()
    return pl.when(s.str.to_lowercase().is_in(NULLS)).then(None).otherwise(s)


def text(col: str, case: str | None = None) -> pl.Expr:
    s = clean(col)
    if case == "upper":
        return s.str.to_uppercase()
    if case == "lower":
        return s.str.to_lowercase()
    return s


def number(col: str, decimal: str = ".", thousands: str | None = None, factor: float = 1.0) -> pl.Expr:
    """'-$1,234.56', '(45.20)', '12,50 €', 'EUR 12.50', '21.4°C' -> float."""
    s = clean(col)
    negative = s.str.contains(r"^\\(|^-|-$|\\)$|^[^0-9]*-")
    digits = s.str.replace_all(r"[^0-9.,]", "")
    if thousands:
        digits = digits.str.replace_all(thousands, "", literal=True)
    if decimal == ",":
        digits = digits.str.replace(",", ".", literal=True)
    value = digits.cast(pl.Float64, strict=False) * factor
    return pl.when(negative).then(-value).otherwise(value)


def whole(col: str, **kw: Any) -> pl.Expr:
    return number(col, **kw).round(0).cast(pl.Int64)


def debit_credit(debit: str, credit: str, **kw: Any) -> pl.Expr:
    return number(credit, **kw).fill_null(0.0) - number(debit, **kw).fill_null(0.0)


def f_to_c(col: str, **kw: Any) -> pl.Expr:
    return (number(col, **kw) - 32) * 5 / 9


def hours_minutes(col: str) -> pl.Expr:
    parts = clean(col).str.split_exact(":", 1)
    return parts.struct.field("field_0").cast(pl.Float64, strict=False) + parts.struct.field("field_1").cast(
        pl.Float64, strict=False) / 60


def date(col: str, fmt: str) -> pl.Expr:
    return clean(col).str.strptime(pl.Date, fmt, strict=False)


def timestamp(col: str, fmt: str) -> pl.Expr:
    if fmt in ("epoch_s", "epoch_ms"):
        unit = "s" if fmt == "epoch_s" else "ms"
        return pl.from_epoch(clean(col).cast(pl.Int64, strict=False), time_unit=unit).cast(pl.Datetime("us"))
    return clean(col).str.strptime(pl.Datetime("us"), fmt, strict=False)


def date_from_timestamp(col: str, fmt: str) -> pl.Expr:
    return timestamp(col, fmt).dt.date()


def combine_date_time(date_col: str, time_col: str, fmt: str) -> pl.Expr:
    joined = pl.concat_str([clean(date_col), clean(time_col)], separator=" ")
    return joined.str.strptime(pl.Datetime("us"), fmt, strict=False)


def name_last_first(col: str) -> pl.Expr:
    return clean(col).str.replace(r"^([^,]+),\\s*(.+)$", "${2} ${1}")


def concat(*cols: str) -> pl.Expr:
    return pl.concat_str([clean(c) for c in cols], separator=" ", ignore_nulls=True)


def phone_e164(col: str, country: str = "64") -> pl.Expr:
    s = clean(col)
    digits = s.str.replace_all(r"[^0-9]", "")
    return (
        pl.when(s.is_null() | (digits == ""))
        .then(None)
        .when(s.str.starts_with("+"))
        .then("+" + digits)
        .when(digits.str.starts_with("0"))
        .then("+" + country + digits.str.slice(1))
        .when(digits.str.starts_with(country))
        .then("+" + digits)
        .otherwise("+" + country + digits)
    )
'''

POLARS_CAST = {
    "string": "pl.String",
    "int": "pl.Int64",
    "decimal": "pl.Float64",
    "float": "pl.Float64",
    "date": "pl.Date",
    "datetime": 'pl.Datetime("us")',
    "bool": "pl.Boolean",
}


def _kw(params: dict[str, Any], keys: tuple[str, ...]) -> str:
    out = []
    for k in keys:
        if k in params and params[k] not in (None, "", 1, 1.0) or (k == "decimal" and params.get(k) == ","):
            if k == "decimal" and params.get(k) == ".":
                continue
            out.append(f"{k}={json.dumps(params[k], ensure_ascii=False)}")
    return ", ".join(out)


def expr(c: ColumnMap) -> str:
    src = [json.dumps(s, ensure_ascii=False) for s in c.source]
    p = c.params
    num = _kw(p, ("decimal", "thousands", "factor"))
    tail = f", {num}" if num else ""
    t = c.transform
    if t == "text":
        case = p.get("case")
        return f"text({src[0]}, case={json.dumps(case)})" if case else f"text({src[0]})"
    if t == "int":
        return f"whole({src[0]}{tail})"
    if t == "number":
        return f"number({src[0]}{tail})"
    if t == "debit_credit":
        return f"debit_credit({src[0]}, {src[1]}{tail})"
    if t == "f_to_c":
        return f"f_to_c({src[0]}{tail})"
    if t == "hours_minutes":
        return f"hours_minutes({src[0]})"
    if t == "date":
        return f"date({src[0]}, {json.dumps(p['format'])})"
    if t == "datetime":
        return f"timestamp({src[0]}, {json.dumps(p['format'])})"
    if t == "date_from_datetime":
        return f"date_from_timestamp({src[0]}, {json.dumps(p['format'])})"
    if t == "combine_date_time":
        return f"combine_date_time({src[0]}, {src[1]}, {json.dumps(p['format'])})"
    if t == "name_last_first":
        return f"name_last_first({src[0]})"
    if t == "concat":
        return f"concat({', '.join(src)})"
    if t == "phone_e164":
        return f"phone_e164({src[0]}, country={json.dumps(str(p.get('country', '64')))})"
    if t == "const":
        return f"pl.lit({json.dumps(p.get('value'), ensure_ascii=False)})"
    raise ValueError(f"unknown transform {t}")


def polars_code(m: Mapping, contract: Contract, title: str = "") -> str:
    lines = []
    for col in contract.columns:
        c = m.col(col.name)
        e = expr(c) if c else "pl.lit(None)"
        if col.type == "decimal":
            e = f"{e}.round(2)"
        lines.append(f"        {col.name}={e}.cast({POLARS_CAST[col.type]}),")
    ignored = "".join(f"\n#   {k}: {v}" for k, v in m.ignore.items())
    header = f'"""{title or "Generated transform"} (compiled from the mapping; no model involved)."""'
    return (
        f"{header}\n\nfrom __future__ import annotations\n\nfrom typing import Any\n\nimport polars as pl\n"
        f"{HELPERS}\n\n# Source columns not loaded, as decided in the mapping:{ignored or chr(10) + '#   (none)'}\n\n\n"
        f"def transform(raw: pl.DataFrame) -> pl.DataFrame:\n    return raw.select(\n" + "\n".join(lines) + "\n    )\n"
    )


# ---------------------------------------------------------------- SQL for the dbt staging model


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(v: Any) -> str:
    return "'" + str(v).replace("'", "''") + "'"


NULLS_SQL = "('', 'na', 'n/a', 'null', 'none', 'nil', '-', '--', 'nan', '?', '#n/a')"


def _clean(col: str) -> str:
    c = _q(col)
    return f"CASE WHEN lower(trim({c})) IN {NULLS_SQL} THEN NULL ELSE trim({c}) END"


def _number(col: str, p: dict[str, Any]) -> str:
    s = _clean(col)
    digits = f"regexp_replace({s}, '[^0-9.,]', '', 'g')"
    if p.get("thousands"):
        digits = f"replace({digits}, {_lit(p['thousands'])}, '')"
    if p.get("decimal") == ",":
        digits = f"replace({digits}, ',', '.')"
    sign = f"CASE WHEN regexp_matches({s}, '^\\(|^-|-$|\\)$|^[^0-9]*-') THEN -1 ELSE 1 END"
    factor = f" * {p['factor']}" if p.get("factor") not in (None, 1, 1.0) else ""
    return f"(TRY_CAST({digits} AS DOUBLE) * {sign}{factor})"


def _strptime(expr_sql: str, fmt: str) -> str:
    if fmt == "epoch_s":
        return f"make_timestamp(TRY_CAST({expr_sql} AS BIGINT) * 1000000)"
    if fmt == "epoch_ms":
        return f"make_timestamp(TRY_CAST({expr_sql} AS BIGINT) * 1000)"
    return f"try_strptime({expr_sql}, {_lit(fmt)})"


def sql_expr(c: ColumnMap) -> str:
    s, p, t = c.source, c.params, c.transform
    if t == "text":
        e = _clean(s[0])
        return f"upper({e})" if p.get("case") == "upper" else f"lower({e})" if p.get("case") == "lower" else e
    if t in ("int", "number"):
        return _number(s[0], p)
    if t == "debit_credit":
        return f"(coalesce({_number(s[1], p)}, 0) - coalesce({_number(s[0], p)}, 0))"
    if t == "f_to_c":
        return f"(({_number(s[0], p)} - 32) * 5 / 9)"
    if t == "hours_minutes":
        e = _clean(s[0])
        return f"(TRY_CAST(split_part({e}, ':', 1) AS DOUBLE) + TRY_CAST(split_part({e}, ':', 2) AS DOUBLE) / 60)"
    if t == "date":
        return f"CAST({_strptime(_clean(s[0]), p['format'])} AS DATE)"
    if t == "datetime":
        return f"CAST({_strptime(_clean(s[0]), p['format'])} AS TIMESTAMP)"
    if t == "date_from_datetime":
        return f"CAST({_strptime(_clean(s[0]), p['format'])} AS DATE)"
    if t == "combine_date_time":
        joined = f"concat_ws(' ', {_clean(s[0])}, {_clean(s[1])})"
        return f"CAST({_strptime(joined, p['format'])} AS TIMESTAMP)"
    if t == "name_last_first":
        return f"regexp_replace({_clean(s[0])}, '^([^,]+),\\s*(.+)$', '\\2 \\1')"
    if t == "concat":
        return "concat_ws(' ', " + ", ".join(_clean(x) for x in s) + ")"
    if t == "phone_e164":
        e = _clean(s[0])
        d = f"regexp_replace({e}, '[^0-9]', '', 'g')"
        cc = _lit(p.get("country", "64"))
        return (
            f"CASE WHEN {e} IS NULL OR {d} = '' THEN NULL WHEN starts_with({e}, '+') THEN '+' || {d} "
            f"WHEN starts_with({d}, '0') THEN '+' || {cc} || substr({d}, 2) "
            f"WHEN starts_with({d}, {cc}) THEN '+' || {d} ELSE '+' || {cc} || {d} END"
        )
    if t == "const":
        return _lit(p.get("value"))
    raise ValueError(t)


SQL_CAST = {
    "string": "VARCHAR",
    "int": "BIGINT",
    "decimal": "DECIMAL(18,2)",
    "float": "DOUBLE",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "bool": "BOOLEAN",
}


def sql_select(m: Mapping, contract: Contract, source_ref: str) -> str:
    cols = []
    for col in contract.columns:
        c = m.col(col.name)
        e = sql_expr(c) if c else "NULL"
        if col.type == "int":
            e = f"round({e})"
        cols.append(f"    CAST({e} AS {SQL_CAST[col.type]}) AS {col.name}")
    return "SELECT\n" + ",\n".join(cols) + f"\nFROM {source_ref}\n"
