"""Profile a raw table: what each column holds, how its values are written, and which columns identify a row.

The profile is evidence, not a decision. It feeds three readers: the rules mapper, the model (as a compact JSON
summary) and the data tests (for example the reconciliation total is computed from the profiled number style,
independently of the generated code).
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from .readers import RawTable

NULL_TOKENS = {"", "na", "n/a", "null", "none", "nil", "-", "--", "nan", "?", "#n/a"}

DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d.%m.%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%Y%m%d",
    "%Y/%m/%d",
    "%d %B %Y",
]
TIME_PARTS = ["%H:%M:%S", "%H:%M"]
DATETIME_FORMATS = [f"{d}{sep}{t}" for d in DATE_FORMATS[:8] for sep in (" ", "T") for t in TIME_PARTS] + [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y%m%dT%H%M%S",
    "%Y-%m-%dT%H:%M:%S%z",
]

CURRENCY = re.compile(
    r"^(?P<pre>NZ\$|A\$|AU\$|US\$|\$|€|£|NZD|AUD|USD|EUR|GBP)?\s*(?P<num>.*?)\s*"
    r"(?P<post>€|NZD|AUD|USD|EUR|GBP)?$"
)
UNIT_SUFFIX = re.compile(r"^(?P<num>[-+(]?[\d.,]+\)?)\s*(?P<unit>°C|°F|C|F|%|V|mV|kg|g|m|cm|mm|L|ml|kWh)$")
HEADER_UNIT = re.compile(
    r"(\((?P<a>[^)]{1,6})\)|\[(?P<b>[^\]]{1,6})\]|°(?P<c>[CF])\b|_(?P<d>f|c|mv|pct|cents?)$|"
    r"\b(?P<e>cents|mv|millivolts|fahrenheit|celsius|percent)\b)",
    re.I,
)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$")
SYMBOL_CCY = {"NZ$": "NZD", "A$": "AUD", "AU$": "AUD", "US$": "USD", "€": "EUR", "£": "GBP", "$": None}


def is_null(v: str | None) -> bool:
    return v is None or v.strip().lower() in NULL_TOKENS


@dataclass
class NumberStyle:
    decimal: str = "."
    thousands: str | None = None
    symbol: str | None = None  # currency symbol or code written with the value
    currency: str | None = None  # ISO code implied by the symbol, when unambiguous
    paren_negative: bool = False
    unit: str | None = None  # unit written inside the values, e.g. °C, %, V


@dataclass
class ColumnProfile:
    name: str
    kind: str
    nulls: int
    distinct: int
    unique: bool
    samples: list[str]
    format: str | None = None
    ambiguous: list[str] = field(default_factory=list)
    number: NumberStyle | None = None
    unit: str | None = None  # unit named in the header, e.g. F in "Temp (F)"
    min: str | None = None
    max: str | None = None
    case: str | None = None

    def brief(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "nulls": self.nulls,
            "distinct": self.distinct,
            "samples": self.samples,
        }
        if self.format:
            d["format"] = self.format
        if self.ambiguous:
            d["also_fits"] = self.ambiguous
        if self.number:
            d["number"] = {k: v for k, v in asdict(self.number).items() if v not in (None, False)}
        for k in ("unit", "min", "max", "case"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        return d


@dataclass
class Profile:
    file: str
    rows: int
    read: dict[str, Any]
    meta: dict[str, str]
    skipped: list[str]
    columns: list[ColumnProfile]
    keys: list[list[str]]

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def col(self, name: str) -> ColumnProfile:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def brief(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "rows": self.rows,
            "read": self.read,
            "meta": self.meta,
            "skipped_lines": self.skipped[:6],
            "columns": [c.brief() for c in self.columns],
            "candidate_keys": self.keys[:5],
        }


# ---------------------------------------------------------------- numbers


def split_number(v: str) -> tuple[str, str | None, str | None, bool, bool] | None:
    """Split '-$1,234.56', '(45.20)', '12,50 €', 'EUR 12.50' or '21.4°C'.

    Returns (digits, symbol, unit, negative, parentheses), or None when the text is not a number.
    """
    s = v.strip()
    neg = paren = False
    if s.startswith("(") and s.endswith(")"):
        neg = paren = True
        s = s[1:-1].strip()
    if s and s[0] in "+-":
        neg = s[0] == "-"
        s = s[1:].strip()
    unit = None
    u = UNIT_SUFFIX.match(s)
    if u:
        s, unit = u.group("num"), u.group("unit")
    m = CURRENCY.match(s)
    if not m:
        return None
    sym = m.group("pre") or m.group("post")
    core = m.group("num").replace(" ", "")
    if core.startswith("(") and core.endswith(")"):
        neg = paren = True
        core = core[1:-1]
    if core and core[0] in "+-":
        neg = core[0] == "-"
        core = core[1:]
    if core.endswith("-"):
        neg, core = True, core[:-1]
    if not (re.fullmatch(r"\d[\d.,]*", core) or re.fullmatch(r"[.,]\d+", core)):
        return None
    return core, sym, unit, neg, paren


def number_style(values: list[str]) -> NumberStyle | None:
    style = NumberStyle()
    cores = []
    symbols: Counter[str] = Counter()
    units: Counter[str] = Counter()
    for v in values:
        parts = split_number(v)
        if parts is None:
            return None
        core, sym, unit, _neg, paren = parts
        if sym:
            symbols[sym] += 1
        if unit:
            units[unit] += 1
        style.paren_negative = style.paren_negative or paren
        cores.append(core)
    if not cores:
        return None
    if symbols:
        style.symbol = symbols.most_common(1)[0][0]
        style.currency = SYMBOL_CCY.get(style.symbol, style.symbol)
    if units:
        style.unit = units.most_common(1)[0][0]
    both = [c for c in cores if "." in c and "," in c]
    if both:
        last = Counter("," if c.rfind(",") > c.rfind(".") else "." for c in both).most_common(1)[0][0]
        style.decimal = last
        style.thousands = "." if last == "," else ","
    else:
        commas = [c for c in cores if "," in c]
        dots = [c for c in cores if "." in c]
        if commas and not dots:
            if any(not re.fullmatch(r"\d{1,3}(,\d{3})+", c) for c in commas):
                style.decimal = ","
            else:
                style.thousands = ","
        elif commas:
            style.thousands = ","
    return style


def parse_number(v: str | None, style: NumberStyle) -> float | None:
    """The reference parser: used by the data tests to reconcile totals independently of the generated code."""
    if v is None or is_null(v):
        return None
    parts = split_number(v)
    if parts is None:
        return None
    core, _sym, _unit, neg, _paren = parts
    if style.thousands:
        core = core.replace(style.thousands, "")
    if style.decimal == ",":
        core = core.replace(",", ".")
    try:
        x = float(core)
    except ValueError:
        return None
    return -x if neg else x


# ---------------------------------------------------------------- dates


def _fits(values: list[str], fmt: str) -> bool:
    for v in values:
        try:
            d = dt.datetime.strptime(v.strip(), fmt)
        except ValueError:
            return False
        if not 1800 <= d.year <= 2200:
            return False
    return True


def date_format(values: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Returns (kind, format, other formats that also fit). Day-first wins a tie (New Zealand convention)."""
    sample = values[:200]
    if all(re.fullmatch(r"\d{10}", v.strip()) for v in sample) and all(9e8 <= int(v) <= 2.2e9 for v in sample):
        return "datetime", "epoch_s", []
    if all(re.fullmatch(r"\d{13}", v.strip()) for v in sample):
        return "datetime", "epoch_ms", []
    for kind, fmts in (("date", DATE_FORMATS), ("datetime", DATETIME_FORMATS)):
        fits = [f for f in fmts if _fits(sample, f)]
        if fits:
            return kind, fits[0], fits[1:]
    return None, None, []


# ---------------------------------------------------------------- columns


def _kind(values: list[str]) -> tuple[str, dict[str, Any]]:
    if not values:
        return "empty", {}
    vs = [v.strip() for v in values]
    kind, fmt, alt = date_format(vs)
    if kind and not (fmt == "%Y%m%d" and len({v[:4] for v in vs}) > 3):
        return kind, {"format": fmt, "ambiguous": alt}
    if all(re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", v) for v in vs):
        return "time", {"format": "%H:%M:%S"}
    if all(re.fullmatch(r"\d{1,2}:[0-5]\d", v) for v in vs):
        return "hours_minutes", {"format": "h:mm"}
    if all(EMAIL.match(v) for v in vs):
        return "email", {}
    low = {v.lower() for v in vs}
    if low <= {"true", "false", "yes", "no", "y", "n", "0", "1"} and low - {"0", "1"}:
        return "bool", {}
    phone = [
        v
        for v in vs
        if re.fullmatch(r"\+?[\d ()]{7,20}", v)
        and 8 <= len(re.sub(r"\D", "", v)) <= 13
        and (v.startswith(("+", "(", "0")) or " " in v)
    ]
    if len(phone) == len(vs):
        return "phone", {}
    style = number_style(vs)
    if style is not None:
        nums = [parse_number(v, style) for v in vs]
        if all(n is not None for n in nums):
            ints = (
                all(float(n).is_integer() for n in nums if n is not None)
                and style.decimal == "."
                and not any("." in v for v in vs)
            )
            if style.symbol:
                return "money", {"number": style}
            if style.unit == "%":
                return "percent", {"number": style}
            return ("int" if ints else "decimal"), {"number": style}
    if all(CODE.match(v) for v in vs) and (any(c.isdigit() for v in vs for c in v) or all(len(v) <= 5 for v in vs)):
        return "code", {}
    return "text", {}


def _case(values: list[str]) -> str | None:
    letters = [v for v in values if any(c.isalpha() for c in v)]
    if not letters:
        return None
    if all(v == v.upper() for v in letters):
        return "upper"
    if all(v == v.lower() for v in letters):
        return "lower"
    return "mixed"


def header_unit(name: str) -> str | None:
    m = HEADER_UNIT.search(name)
    if not m:
        return None
    u = next(g for g in m.groups()[1:] if g)
    u = u.strip().lower()
    return {
        "fahrenheit": "f",
        "celsius": "c",
        "millivolts": "mv",
        "percent": "%",
        "pct": "%",
        "cent": "cents",
        "°f": "f",
        "°c": "c",
    }.get(u, u)


def profile_column(name: str, raw: list[str | None]) -> ColumnProfile:
    values = [v for v in raw if v is not None and not is_null(v)]
    nulls = len(raw) - len(values)
    kind, extra = _kind(values)
    distinct = len(set(values))
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
        if len(seen) == 5:
            break
    p = ColumnProfile(
        name=name,
        kind=kind,
        nulls=nulls,
        distinct=distinct,
        unique=nulls == 0 and distinct == len(raw) and len(raw) > 0,
        samples=[s[:60] for s in seen],
        format=extra.get("format"),
        ambiguous=extra.get("ambiguous", []),
        number=extra.get("number"),
        unit=header_unit(name),
    )
    if kind in ("int", "decimal", "money", "percent") and p.number:
        nums = [parse_number(v, p.number) for v in values]
        clean = [n for n in nums if n is not None]
        if clean:
            p.min, p.max = _num_text(min(clean)), _num_text(max(clean))
    elif kind in ("date", "datetime") and p.format and not p.format.startswith("epoch"):
        parsed = sorted(dt.datetime.strptime(v.strip(), p.format) for v in values if v)
        if parsed:
            p.min, p.max = parsed[0].isoformat(sep=" "), parsed[-1].isoformat(sep=" ")
    if kind in ("code", "text"):
        p.case = _case([v for v in values if v])
    return p


def _num_text(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(round(x, 6))


def candidate_keys(df_cols: dict[str, list[str | None]], profiles: list[ColumnProfile]) -> list[list[str]]:
    n = len(next(iter(df_cols.values()), []))
    if n == 0:
        return []
    keys: list[list[str]] = [[p.name] for p in profiles if p.unique and p.kind in ("code", "int", "text", "email")]
    pool = [
        p.name
        for p in profiles
        if p.nulls == 0 and p.kind in ("code", "int", "date", "datetime", "text") and p.distinct < n
    ][:9]
    for size in (2, 3):
        if keys:
            break
        for combo in itertools.combinations(pool, size):
            rows = set(zip(*(df_cols[c] for c in combo), strict=True))
            if len(rows) == n:
                keys.append(list(combo))
    return keys[:6]


def profile(raw: RawTable) -> Profile:
    cols = {c: raw.df[c].to_list() for c in raw.df.columns}
    profiles = [profile_column(c, v) for c, v in cols.items()]  # "@" columns carry file-level values
    return Profile(
        file=raw.path.name,
        rows=raw.df.height,
        read=raw.spec.to_dict(),
        meta=dict(raw.meta),
        skipped=list(raw.skipped),
        columns=profiles,
        keys=candidate_keys(
            {p.name: cols[p.name] for p in profiles if not p.name.startswith("@")},
            [p for p in profiles if not p.name.startswith("@")],
        ),
    )
