"""The workspace: versioned mappings and code on disk, and the DuckDB warehouse with its quarantine and audit tables.

Layout (under ETL_HOME, default .etl/):
    sources/<source>/mapping.v<N>.json   mapping, fingerprint of the file it was made for, status, drift found
    sources/<source>/code/<sha>.py       every transform version that was tested
    warehouse.duckdb                     the tables, raw.<source>, etl.quarantine, etl.runs

Mappings and code are plain files so they can be reviewed, diffed and committed like any other code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from .contracts import DUCKDB_TYPES, Contract
from .mapping import Mapping, from_dict
from .paths import home


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "source"


def default_source(path: Path) -> str:
    """A source name from a file name, without dates and counters: harbour_bank_2026-03.csv -> harbour_bank."""
    stem = re.sub(r"[-_ ]?(\d{4}[-_]?\d{2}([-_]?\d{2})?|\d+|week\d+|q\d)$", "", Path(path).stem.lower())
    return slug(stem)


class Workspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or home())
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ mappings and code

    def source_dir(self, source: str) -> Path:
        d = self.root / "sources" / slug(source)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sources(self) -> list[str]:
        d = self.root / "sources"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def versions(self, source: str) -> list[dict[str, Any]]:
        d = self.root / "sources" / slug(source)
        if not d.exists():
            return []
        files = sorted(d.glob("mapping.v*.json"), key=lambda p: int(p.stem.split(".v")[1]))
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def save_mapping(
        self,
        source: str,
        m: Mapping,
        fingerprint: dict[str, Any],
        file: str,
        file_sha: str,
        drift: list[dict[str, Any]] | None = None,
        status: str = "proposed",
        path: Path | None = None,
    ) -> int:
        version = len(self.versions(source)) + 1
        record = {
            "source": slug(source),
            "version": version,
            "status": status,
            "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "file": file,
            "file_sha256": file_sha,
            "path": str(Path(path).resolve()) if path else None,
            "mapping": m.to_dict(),
            "fingerprint": fingerprint,
            "drift": drift or [],
        }
        path = self.source_dir(source) / f"mapping.v{version}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return version

    def update_mapping(self, source: str, version: int, m: Mapping | None = None, status: str | None = None) -> None:
        path = self.source_dir(source) / f"mapping.v{version}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if m is not None:
            record["mapping"] = m.to_dict()
            record["edited"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        if status is not None:
            record["status"] = status
            if status == "approved":
                record["approved"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def set_promoted(self, source: str, version: int, code_sha: str) -> None:
        """Remember the code version that passed every test for this mapping version."""
        path = self.source_dir(source) / f"mapping.v{version}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["promoted_code"] = code_sha
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def record(self, source: str, version: int | None = None, approved_only: bool = False) -> dict[str, Any] | None:
        vs = self.versions(source)
        if version is not None:
            return next((v for v in vs if v["version"] == version), None)
        if approved_only:
            vs = [v for v in vs if v["status"] == "approved"]
        return vs[-1] if vs else None

    def mapping(
        self, source: str, version: int | None = None, approved_only: bool = False
    ) -> tuple[Mapping, dict[str, Any]] | None:
        r = self.record(source, version, approved_only)
        return (from_dict(r["mapping"]), r) if r else None

    def save_code(self, source: str, code: str) -> str:
        sha = hashlib.sha256(code.encode()).hexdigest()[:12]
        d = self.source_dir(source) / "code"
        d.mkdir(exist_ok=True)
        (d / f"{sha}.py").write_text(code, encoding="utf-8")
        return sha

    def code(self, source: str, sha: str) -> str | None:
        p = self.source_dir(source) / "code" / f"{sha}.py"
        return p.read_text(encoding="utf-8") if p.exists() else None

    # ------------------------------------------------------------ warehouse

    @property
    def db_path(self) -> Path:
        return self.root / "warehouse.duckdb"

    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        if read_only and not self.db_path.exists():
            self.connect().close()
        con = duckdb.connect(str(self.db_path), read_only=read_only)
        if not read_only:
            con.execute("CREATE SCHEMA IF NOT EXISTS raw; CREATE SCHEMA IF NOT EXISTS etl;")
            con.execute("""CREATE TABLE IF NOT EXISTS etl.runs (
                run_id VARCHAR, started_at TIMESTAMP, source VARCHAR, file VARCHAR, file_sha256 VARCHAR,
                table_name VARCHAR, mapping_version INTEGER, mapping_author VARCHAR, code_sha VARCHAR,
                engine VARCHAR, status VARCHAR, repair_rounds INTEGER, rows_in INTEGER, rows_loaded INTEGER,
                rows_quarantined INTEGER, lines_skipped INTEGER, tests_passed INTEGER, tests_failed INTEGER,
                model_calls INTEGER, tests JSON, drift JSON, log JSON)""")
            con.execute("""CREATE TABLE IF NOT EXISTS etl.quarantine (
                run_id VARCHAR, table_name VARCHAR, file VARCHAR, file_row INTEGER, reasons VARCHAR, record JSON)""")
        return con

    @staticmethod
    def _insert(con: duckdb.DuckDBPyConnection, table: str, df: pl.DataFrame) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rows.parquet"
            df.write_parquet(p)
            cols = ", ".join(f'"{c}"' for c in df.columns)
            con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM read_parquet(?)", [str(p)])

    def ensure_table(self, con: duckdb.DuckDBPyConnection, contract: Contract) -> None:
        cols = ", ".join(f'"{c.name}" {DUCKDB_TYPES[c.type]}' for c in contract.columns)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{contract.table}" ({cols}, _run_id VARCHAR, _file_sha256 VARCHAR)')

    def load(
        self,
        *,
        source: str,
        file: str,
        file_sha: str,
        contract: Contract,
        rows: pl.DataFrame,
        quarantined: pl.DataFrame,
        raw: pl.DataFrame,
        run: dict[str, Any],
    ) -> str:
        """Write one promoted run: clean rows, quarantined rows, the raw text table (for dbt) and the audit row."""
        run_id = run.get("run_id") or uuid.uuid4().hex[:12]
        con = self.connect()
        try:
            con.execute("BEGIN")
            self.ensure_table(con, contract)
            # Re-running the same file replaces its rows instead of duplicating them.
            con.execute(f'DELETE FROM "{contract.table}" WHERE _file_sha256 = ?', [file_sha])
            con.execute("DELETE FROM etl.quarantine WHERE file = ? AND table_name = ?", [file, contract.table])
            tagged = rows.with_columns(pl.lit(run_id).alias("_run_id"), pl.lit(file_sha).alias("_file_sha256"))
            self._insert(con, f'"{contract.table}"', tagged)
            if quarantined.height:
                records = [json.dumps(r, default=str) for r in quarantined.drop("row", "reasons").to_dicts()]
                q = pl.DataFrame(
                    {
                        "run_id": [run_id] * quarantined.height,
                        "table_name": [contract.table] * quarantined.height,
                        "file": [file] * quarantined.height,
                        "file_row": quarantined["row"].cast(pl.Int32),
                        "reasons": quarantined["reasons"],
                        "record": records,
                    }
                )
                self._insert(con, "etl.quarantine", q)
            rawname = f'raw."{slug(source)}"'
            con.execute(f"DROP TABLE IF EXISTS {rawname}")
            cols = ", ".join(f'"{c}" VARCHAR' for c in raw.columns)
            con.execute(f"CREATE TABLE {rawname} ({cols})")
            if raw.height:
                self._insert(con, rawname, raw)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            con.close()
            raise
        con.close()
        return run_id

    def audit(self, run: dict[str, Any]) -> str:
        run_id = run.setdefault("run_id", uuid.uuid4().hex[:12])
        con = self.connect()
        cols = [
            "run_id",
            "started_at",
            "source",
            "file",
            "file_sha256",
            "table_name",
            "mapping_version",
            "mapping_author",
            "code_sha",
            "engine",
            "status",
            "repair_rounds",
            "rows_in",
            "rows_loaded",
            "rows_quarantined",
            "lines_skipped",
            "tests_passed",
            "tests_failed",
            "model_calls",
            "tests",
            "drift",
            "log",
        ]
        vals = [json.dumps(run.get(c), default=str) if c in ("tests", "drift", "log") else run.get(c) for c in cols]
        con.execute(f"INSERT INTO etl.runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        con.close()
        return run_id

    def query(self, sql: str, params: list[Any] | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
        con = self.connect(read_only=True)
        try:
            cur = con.execute(sql, params or [])
            names = [d[0] for d in cur.description] if cur.description else []
            return names, cur.fetchall()
        finally:
            con.close()
