"""Child process for the sandbox: load transform.py, run it on in.arrow, write out.arrow, report errors as JSON.

Error text is kept short and free of machine-specific paths, so the same failure gives the same repair prompt on
any machine (which keeps recorded model replies replayable).
"""

import io
import json
import sys
import traceback

import polars as pl


def main() -> None:
    code_path, in_path, out_path = sys.argv[1:4]
    code = open(code_path, encoding="utf-8").read()
    lines = code.splitlines()
    try:
        scope: dict = {"__name__": "transform"}
        exec(compile(code, "transform.py", "exec"), scope)
        raw = pl.read_ipc(io.BytesIO(open(in_path, "rb").read()))
        out = scope["transform"](raw)
        if not isinstance(out, pl.DataFrame):
            raise TypeError(f"transform returned {type(out).__name__}, not a polars DataFrame")
        out.write_ipc(out_path)
    except Exception as e:  # noqa: BLE001 - every failure is reported to the parent
        line = None
        for fr in traceback.extract_tb(e.__traceback__):
            if fr.filename == "transform.py":
                line = fr.lineno
        msg = str(e).strip().splitlines()
        text = f"{type(e).__name__}: {' '.join(m.strip() for m in msg[:6])[:500]}"
        if line:
            text += f" (transform.py line {line}: {lines[line - 1].strip()[:160]})"
        print(json.dumps({"error": text}))
        sys.exit(1)


main()
