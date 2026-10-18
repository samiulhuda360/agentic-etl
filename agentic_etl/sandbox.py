"""Run a generated transform in a separate, isolated Python process.

Two layers keep generated code in its lane:
1. a static check of the code (imports limited to polars, datetime, re, math, typing; no file, network, process
   or reflection calls; no Polars readers or writers);
2. a child process started with `python -I` in an empty temporary folder, a minimal environment and a time limit.
   The raw table goes in and the result comes out as Arrow IPC files.

This is a guard rail against mistakes, not a hostile-code jail. For untrusted tenants, run the child in a
container with no network as well.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import polars as pl

ALLOWED_IMPORTS = {"polars", "datetime", "re", "math", "typing", "__future__", "decimal"}
BANNED_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "getattr",
    "setattr",
    "delattr",
    "memoryview",
    "help",
    "exit",
    "quit",
}
BANNED_ATTR_PREFIX = ("read_", "write_", "scan_", "sink_", "to_pandas", "from_pandas", "__")
RUNNER = Path(__file__).with_name("_child.py")


@dataclass
class Result:
    ok: bool
    df: pl.DataFrame | None
    error: str | None
    seconds: float


def check(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e.msg} (line {e.lineno})"]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    out.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                out.append(f"line {node.lineno}: import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            out.append(f"line {node.lineno}: '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith(BANNED_ATTR_PREFIX):
            out.append(f"line {node.lineno}: '.{node.attr}' is not allowed")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "transform" for n in tree.body):
        out.append("the code must define transform(raw: pl.DataFrame) -> pl.DataFrame")
    return out


def run(code: str, raw: pl.DataFrame, timeout: float = 60.0) -> Result:
    started = time.perf_counter()
    issues = check(code)
    if issues:
        return Result(False, None, "Rejected before running: " + "; ".join(issues), 0.0)
    with tempfile.TemporaryDirectory(prefix="etl-sandbox-") as tmp:
        d = Path(tmp)
        (d / "transform.py").write_text(code, encoding="utf-8")
        raw.write_ipc(d / "in.arrow")
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
        env["POLARS_MAX_THREADS"] = "2"
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(RUNNER), "transform.py", "in.arrow", "out.arrow"],
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Result(False, None, f"Timed out after {timeout:.0f} s", time.perf_counter() - started)
        secs = round(time.perf_counter() - started, 3)
        if proc.returncode != 0 or not (d / "out.arrow").exists():
            try:
                err = json.loads(proc.stdout.strip().splitlines()[-1])["error"]
            except (IndexError, ValueError, KeyError):
                err = (proc.stderr or proc.stdout or "the child process failed").strip()[-600:]
            return Result(False, None, err, secs)
        return Result(True, pl.read_ipc(io.BytesIO((d / "out.arrow").read_bytes())), None, secs)
