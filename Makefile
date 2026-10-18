# One command to set up, one to run the demo. On Windows without make, run the commands after each colon.
setup:
	python -m pip install -e ".[dev]"

demo:
	etl demo

serve:
	etl --home .etl/demo serve

test:
	ruff check . && ruff format --check . && mypy && python -m pytest -q

eval:
	etl eval

replay:
	etl eval --offline --check eval/results.json

data:
	python datasets/build.py

.PHONY: setup demo serve test eval replay data
