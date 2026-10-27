.PHONY: help install pipeline run dev test backtest sample demo clean check-data

PY ?= python
PORT ?= 8000

help:
	@echo "make install    install the package + dev tools"
	@echo "make pipeline   build data/quote.duckdb from data/raw/*.xlsx|csv (about 1-2 min)"
	@echo "make run        start the API + quote page on :$(PORT)"
	@echo "make test       run the test-suite (synthetic data, no company data needed)"
	@echo "make backtest   train on data before 2026-01-01, test on later jobs"
	@echo "make demo       build from synthetic sample data and start the API"

install:
	$(PY) -m pip install -e ".[dev]"

check-data:
	@ls data/raw/workorder* >/dev/null 2>&1 || (echo "\n  Put workorders.xlsx (or .csv) in data/raw/ first - see README\n" && exit 1)
	@ls data/raw/part* >/dev/null 2>&1 || (echo "\n  Put parts.xlsx (or .csv) in data/raw/ first - see README\n" && exit 1)

pipeline: check-data
	$(PY) -m quote_system.pipeline

run:
	$(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT)

dev:
	$(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT) --reload

test:
	$(PY) -m pytest -q

backtest:
	$(PY) -m quote_system.backtest --cutoff 2026-01-01

sample:
	$(PY) -m quote_system.sample_data data/raw-sample

demo: sample
	QS_RAW_DIR=data/raw-sample QS_WORK_DIR=data/work-sample QS_DB_PATH=data/sample.duckdb $(PY) -m quote_system.pipeline
	QS_DB_PATH=data/sample.duckdb $(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT)

clean:
	rm -rf data/work data/work-sample data/*.duckdb data/*.wal data/backtest_report.md .pytest_cache
