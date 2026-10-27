.PHONY: help install deps lint docker-build docker-pipeline docker-up pipeline run dev test backtest sample demo clean check-data

PY ?= python
PORT ?= 8000

help:
	@echo "make install    install the package + dev tools"
	@echo "make pipeline   build data/quote.duckdb from data/raw/*.xlsx|csv (about 2 min)"
	@echo "make run        start the API + quote page on :$(PORT)"
	@echo "make test       run the test-suite (synthetic data, no company data needed)"
	@echo "make backtest   train on data before 2026-01-01, test on later jobs"
	@echo "make demo       build from synthetic sample data and start the API"
	@echo "make lint       ruff checks"
	@echo "make docker-build / docker-pipeline / docker-up   production container (see README)"

install:
	$(PY) -m pip install -e ".[dev]"

# installs the package only if it is missing (e.g. the Codespace setup step did not run)
deps:
	@$(PY) scripts/check_deps.py || \
		(echo "Installing quote_system and dependencies..." && $(PY) -m pip install -q -e ".[dev]")

check-data:
	@ls data/raw/workorder* >/dev/null 2>&1 || (echo "\n  Put workorders.xlsx (or .csv) in data/raw/ first - see README\n" && exit 1)
	@ls data/raw/part* >/dev/null 2>&1 || (echo "\n  Put parts.xlsx (or .csv) in data/raw/ first - see README\n" && exit 1)

pipeline: deps check-data
	$(PY) -m quote_system.pipeline

run: deps
	$(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT)

dev: deps
	$(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT) --reload

test: deps
	$(PY) -m pytest -q

backtest: deps
	$(PY) -m quote_system.backtest --cutoff 2026-01-01 --similar-sample 1500

sample: deps
	$(PY) -m quote_system.sample_data data/raw-sample

demo: sample
	QS_RAW_DIR=data/raw-sample QS_WORK_DIR=data/work-sample QS_DB_PATH=data/sample.duckdb $(PY) -m quote_system.pipeline
	QS_DB_PATH=data/sample.duckdb $(PY) -m uvicorn quote_system.api.main:app --host 0.0.0.0 --port $(PORT)

clean:
	rm -rf data/work data/work-sample data/*.duckdb data/*.wal data/backtest_report.md .pytest_cache

lint: deps
	$(PY) -m ruff check src tests scripts

docker-build:
	docker compose build

docker-pipeline:
	docker compose run --rm pipeline

docker-up:
	docker compose up -d api
