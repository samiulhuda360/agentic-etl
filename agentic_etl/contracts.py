"""Warehouse table contracts: the columns, types and rules each target table promises its readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from .paths import contracts_dir

TYPES = ("string", "int", "decimal", "float", "date", "datetime", "bool")

POLARS_TYPES: dict[str, Any] = {
    "string": pl.String,
    "int": pl.Int64,
    "decimal": pl.Float64,  # money: Float64 rounded to 2 places
    "float": pl.Float64,
    "date": pl.Date,
    "datetime": pl.Datetime("us"),
    "bool": pl.Boolean,
}

DUCKDB_TYPES = {
    "string": "VARCHAR",
    "int": "BIGINT",
    "decimal": "DECIMAL(18,2)",
    "float": "DOUBLE",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "bool": "BOOLEAN",
}


@dataclass
class Column:
    name: str
    type: str
    description: str = ""
    nullable: bool = False
    aliases: list[str] = field(default_factory=list)
    accepted: list[str] | None = None
    range: tuple[Any, Any] | None = None
    reconcile: bool = False
    tolerance: float | None = None
    case: str | None = None  # "upper" or "lower": the warehouse stores codes in one case
    format: str | None = None  # a written form the column must follow, e.g. "e164" for phone numbers

    def tol(self) -> float:
        if self.tolerance is not None:
            return self.tolerance
        return 0.005 if self.type == "decimal" else 1e-6


@dataclass
class Contract:
    table: str
    description: str
    key: list[str]
    columns: list[Column]
    new: bool = False  # proposed by the mapper, not yet in the catalogue

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def col(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def schema(self) -> dict[str, Any]:
        return {c.name: POLARS_TYPES[c.type] for c in self.columns}

    def to_dict(self) -> dict[str, Any]:
        cols = []
        for c in self.columns:
            d: dict[str, Any] = {"name": c.name, "type": c.type}
            if c.description:
                d["description"] = c.description
            if c.nullable:
                d["nullable"] = True
            if c.aliases:
                d["aliases"] = c.aliases
            if c.accepted:
                d["accepted"] = c.accepted
            if c.range:
                d["range"] = list(c.range)
            if c.reconcile:
                d["reconcile"] = True
            if c.tolerance is not None:
                d["tolerance"] = c.tolerance
            if c.case:
                d["case"] = c.case
            if c.format:
                d["format"] = c.format
            cols.append(d)
        return {"table": self.table, "description": self.description, "key": self.key, "columns": cols}

    def brief(self) -> dict[str, Any]:
        """What the model sees: names, types, descriptions and rules, but not the alias lists."""
        out = []
        for c in self.columns:
            d: dict[str, Any] = {"name": c.name, "type": c.type, "description": c.description}
            if c.nullable:
                d["nullable"] = True
            if c.accepted:
                d["accepted"] = c.accepted
            if c.range:
                d["range"] = list(c.range)
            if c.case:
                d["case"] = c.case
            if c.format:
                d["format"] = c.format
            out.append(d)
        return {"table": self.table, "description": self.description, "key": self.key, "columns": out}


def from_dict(d: dict[str, Any], new: bool = False) -> Contract:
    cols = []
    for c in d["columns"]:
        if c["type"] not in TYPES:
            raise ValueError(f"{d['table']}.{c['name']}: unknown type {c['type']!r}")
        rng = c.get("range")
        cols.append(
            Column(
                name=c["name"],
                type=c["type"],
                description=c.get("description", ""),
                nullable=bool(c.get("nullable", False)),
                aliases=list(c.get("aliases", [])),
                accepted=c.get("accepted"),
                range=(rng[0], rng[1]) if rng else None,
                reconcile=bool(c.get("reconcile", False)),
                tolerance=c.get("tolerance"),
                case=c.get("case"),
                format=c.get("format"),
            )
        )
    return Contract(d["table"], d.get("description", ""), list(d.get("key", [])), cols, new=new)


def load(path: Path) -> Contract:
    return from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def catalogue(directory: Path | None = None) -> dict[str, Contract]:
    directory = directory or contracts_dir()
    out = {}
    for p in sorted(directory.glob("*.yml")):
        c = load(p)
        out[c.table] = c
    return out
