"""The review page: a small standard-library HTTP server over the workspace (no web framework needed).

    GET  /                                  the page
    GET  /api/sources                       sources with their mapping versions and latest run
    GET  /api/source/<s>                    every mapping version, the runs and the latest code
    GET  /api/contracts                     the warehouse tables (for the editor)
    GET  /api/quarantine/<run_id>           rows held back by a run
    POST /api/source/<s>/edit/<v>           save an edited mapping as a new version (author: person)
    POST /api/source/<s>/test/<v>           generate and test mapping v against the file it was made for
    POST /api/source/<s>/approve/<v>        approve mapping v

It binds to 127.0.0.1 only. There is no login: it is a local review tool, not a shared service.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..contracts import catalogue
from ..mapping import VOCABULARY, from_dict, problems
from ..pipeline import onboard
from ..service import Context
from ..store import Workspace

PAGE = Path(__file__).with_name("index.html")


def _jsonable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


class App:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def runs(self, source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not self.ws.db_path.exists():
            return []
        sql = (
            "SELECT run_id, started_at, source, file, file_sha256, table_name, mapping_version, mapping_author, "
            "code_sha, engine, status, repair_rounds, rows_in, rows_loaded, rows_quarantined, lines_skipped, "
            "tests_passed, tests_failed, model_calls, tests, drift, log FROM etl.runs"
        )
        params: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        sql += f" ORDER BY started_at DESC LIMIT {int(limit)}"
        names, rows = self.ws.query(sql, params)
        out = []
        for r in rows:
            d = dict(zip(names, _jsonable(list(r)), strict=True))
            for k in ("tests", "drift", "log"):
                d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
            out.append(d)
        return out

    def sources(self) -> list[dict[str, Any]]:
        out = []
        for s in self.ws.sources():
            vs = self.ws.versions(s)
            if not vs:
                continue
            last = self.runs(s, 1)
            out.append(
                {
                    "source": s,
                    "table": vs[-1]["mapping"]["table"],
                    "versions": len(vs),
                    "approved": max((v["version"] for v in vs if v["status"] == "approved"), default=None),
                    "latest": {k: vs[-1][k] for k in ("version", "status", "file", "created")},
                    "drift": bool(vs[-1].get("drift")),
                    "last_run": last[0] if last else None,
                }
            )
        return out

    def source(self, s: str) -> dict[str, Any]:
        runs = self.runs(s)
        code = self.ws.code(s, runs[0]["code_sha"]) if runs and runs[0].get("code_sha") else None
        return {"source": s, "versions": self.ws.versions(s), "runs": runs, "code": code}

    def quarantine(self, run_id: str) -> list[dict[str, Any]]:
        names, rows = self.ws.query(
            "SELECT file_row, reasons, record FROM etl.quarantine WHERE run_id = ? ORDER BY file_row", [run_id]
        )
        return [{"row": r[0], "reasons": r[1], "record": json.loads(r[2])} for r in rows]

    def edit(self, s: str, v: int, body: dict[str, Any]) -> dict[str, Any]:
        rec = self.ws.record(s, v)
        if rec is None:
            raise KeyError(f"{s} v{v}")
        m = from_dict({**body, "author": "person"})
        nv = self.ws.save_mapping(
            s,
            m,
            rec["fingerprint"],
            rec["file"],
            rec["file_sha256"],
            rec.get("drift"),
            path=Path(rec["path"]) if rec.get("path") else None,
        )
        return {"version": nv}

    def test(self, s: str, v: int) -> dict[str, Any]:
        rec = self.ws.record(s, v)
        if rec is None or not rec.get("path") or not Path(rec["path"]).exists():
            raise KeyError("the file this mapping was made for is not available to re-test")
        m = from_dict(rec["mapping"])
        ctx = Context(ws=self.ws)
        o = onboard(Path(rec["path"]), ctx.with_new(m), "rules", None, mapping=m)
        for a in o.attempts:
            self.ws.save_code(s, a.code)
        rep = o.final.report if o.final else None
        loaded, quarantined = o.split()
        return {
            "status": o.status,
            "log": o.log,
            "tests": [t.to_dict() for t in rep.tests] if rep else [],
            "rows_loaded": loaded.height if loaded is not None else 0,
            "rows_quarantined": quarantined.height if quarantined is not None else 0,
            "code": o.final.code if o.final else None,
        }

    def check(self, s: str, v: int) -> list[str]:
        rec = self.ws.record(s, v)
        if rec is None:
            return ["unknown version"]
        m = from_dict(rec["mapping"])
        contract = Context(ws=self.ws).with_new(m).get(m.table)
        if contract is None:
            return [f"unknown table {m.table}"]
        return problems(m, contract, [c["name"] for c in rec["fingerprint"]["columns"]])


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(_jsonable(body), default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = unquote(self.path.split("?")[0])
            try:
                if path in ("/", "/index.html"):
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/sources":
                    self._send(200, app.sources())
                elif path == "/api/contracts":
                    self._send(
                        200, {"tables": {k: c.to_dict() for k, c in catalogue().items()}, "vocabulary": VOCABULARY}
                    )
                elif path == "/api/runs":
                    self._send(200, app.runs())
                elif m := re.fullmatch(r"/api/source/([\w-]+)", path):
                    self._send(200, app.source(m.group(1)))
                elif m := re.fullmatch(r"/api/quarantine/(\w+)", path):
                    self._send(200, app.quarantine(m.group(1)))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": str(e)})

        def do_POST(self) -> None:  # noqa: N802
            path = unquote(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                if m := re.fullmatch(r"/api/source/([\w-]+)/edit/(\d+)", path):
                    self._send(200, app.edit(m.group(1), int(m.group(2)), body))
                elif m := re.fullmatch(r"/api/source/([\w-]+)/test/(\d+)", path):
                    self._send(200, app.test(m.group(1), int(m.group(2))))
                elif m := re.fullmatch(r"/api/source/([\w-]+)/approve/(\d+)", path):
                    issues = app.check(m.group(1), int(m.group(2)))
                    if issues:
                        self._send(HTTPStatus.CONFLICT, {"error": "; ".join(issues)})
                        return
                    app.ws.update_mapping(m.group(1), int(m.group(2)), status="approved")
                    self._send(200, {"approved": int(m.group(2))})
                else:
                    self._send(404, {"error": "not found"})
            except KeyError as e:
                self._send(404, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": str(e)})

    return Handler


def serve(ws: Workspace, port: int = 8765) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(App(ws)))
    print(f"review page on http://127.0.0.1:{port}  (workspace {ws.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
