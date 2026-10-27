# Service quote system

Quote labour hours and a parts kit for a job (e.g. **Battery R&R on a JD 333G**) from historical work-order segments and the parts used on them.

* **Python + DuckDB** offline pipeline: Excel/CSV exports → part taxonomy → labelled segments → statistics + context-search index (about 2 minutes for 1M segments / 2.6M part lines).
* **FastAPI** service with a quoting page at `/` and Swagger docs at `/docs`.
* Runs in **GitHub Codespaces** with no extra services. The statistics database is a single DuckDB file.

---

## Quick start (Codespaces)

1. **Open the Codespace**: *Code → Codespaces → Create codespace on main*. Dependencies install automatically (≈1–2 min). A 2-core / 8 GB machine is enough.
2. **Add your data.** Company data is never committed (`.gitignore` blocks `*.xlsx`, `*.csv`, `*.parquet`, `*.duckdb`). Put the exports in `data/raw/`:
   ```
   data/raw/workorders.xlsx      # any file starting with "workorder", .xlsx or .csv
   data/raw/parts.xlsx           # any file starting with "part"; all sheets are read (2023, 2024, …)
   ```
   Either drag the files onto the `data/raw` folder in the Explorer panel, or from your own computer run:
   ```bash
   gh codespace cp -e ./workorders.xlsx ./parts.xlsx 'remote:/workspaces/quote-system/data/raw/'
   ```
3. **Build and run:**
   ```bash
   make pipeline     # ≈2 min, writes data/quote.duckdb + search index
   make run          # API + quote page on port 8000 (Codespaces opens it for you)
   ```
4. Optional:
   ```bash
   make test         # 15 tests on synthetic data - no company data needed
   make backtest     # learn from data before 2026-01-01, score quotes on later jobs
   make demo         # try everything on generated sample data
   ```

To refresh with a new export, replace the files in `data/raw/` and run `make pipeline` again. Then restart `make run`, or `POST /admin/reload`.

---

## What a quote looks like

```bash
curl -s -X POST localhost:8000/quotes -H 'content-type: application/json' \
  -d '{"make":"JD","model":"333G","job_text":"replace battery"}'
```

```jsonc
{
  "jobs": ["BATTERY"],
  "total":  {"p25_hours": …, "p50_hours": …, "p80_hours": …, "p90_hours": …, "method": "single_job"},
  "labor":  [{"display_name": "Battery R&R", "level_used": "model", "sample_size": …, "confidence": "high",
              "basis": [{"level": "model", "key": "JD|333G", "n": …}, {"level": "base", "key": "JD|333"}, …]}],
  "parts":  [{"job_code": "BATTERY", "kit": [{"part_number": "…", "qty": 1, "attach_rate": …}], "optional": […]}],
  "similar_work_orders": [ …5 most recent battery jobs on this model… ],
  "quote_id": "…", "warnings": []
}
```

* **P50** is the typical job. **P80** is a safe quote: in backtests about 80 % of real jobs came in at or under it.
* `job_text` is free text, in English or French, and is matched by **meaning**, not word for word. `"VALVE LASH ADJUSTMENT"` also finds past segments written `"SET ENGINE VALVES"`, `"ADJUST VALVE CLEARANCE"`, `"PERFORM VALVE SET"` and `"AJUSTEMENT DES VALVES"`. It works for any job that appears in your history, not only the job list.
* `match`: `auto` (default) uses the context match when past work matches well, otherwise the fixed job list. `context` forces the context match; `job` uses only the job list (battery, starter, PM_500 …). The other method's answer is attached for comparison (`job_list_check` / `context_check`).
* **Parts only.** Send `part_numbers` with no description, e.g. `{"make":"JD","model":"333G","part_numbers":["TY25879B"]}`. The quote comes from past segments that used the same parts. Parts are weighted by how much they say about the job: a starter identifies the repair, an o-ring barely does. The response shows what those parts were used for ("REPLACE BATTERY", "D/R NO START" …).
  - Parts for two repairs (battery + starter) are quoted per repair, then combined using past combined jobs.
  - Core returns (`CR…`) follow their part.
  - Unknown part numbers are listed and ignored.
  - Adding `job_text` as well narrows the match.
  - **Suggested job description:** `suggested_descriptions` gives the most common wording of the matched past jobs. Wordings that name the repair ("REPLACE BATTERY") are preferred over symptoms ("D/R NO START"); one-off and warranty/invoice notes are dropped. Other common wordings of the same job are listed too. For two repairs it combines them ("REPLACE BATTERY & STARTER"). On the page you can **Copy** it or **Use & re-quote**.
* `include_parts: false` covers customer-supplied parts. Labour is unchanged and the kit is dropped.
* **Symptoms.** `"machine not starting"` is quoted from past no-start calls, and the response also includes a diagnosis: on similar machines, what share of no-start segments were fixed by a battery, a starter, a fuel pump …, and how many had no part-identified fix. Each fix has its own hours and can be quoted with one click on the page.
* Every quote is saved (`data/quotes.sqlite`). Record what the job really took with `POST /quotes/{id}/actual`. `GET /accuracy` tracks how quotes compare with actuals.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/quotes` | Quote a job (free text or job codes) on a make/model |
| GET | `/parts/lookup?part_numbers=TY25879B,SE501406` | Check part numbers: description, role, repair, history count |
| GET | `/similar?q=valve lash adjustment&make=JD&model=770GP` | Context match only: matched past descriptions, hours, parts |
| GET | `/quotes/{id}` · POST `/quotes/{id}/actual` | Read a saved quote / record actual hours |
| GET | `/diagnose?symptom=no start&make=JD&model=333G` | Which repairs usually fix a symptom |
| GET | `/jobs?q=batt` · `/makes` · `/models?make=JD&q=333` | Search / autocomplete |
| GET | `/health` · `/meta` · `/accuracy` | Status, build info, quote accuracy |
| POST | `/admin/reload` | Reload after re-running the pipeline |

---

## How it works

```
data/raw/*.xlsx|csv
   │ ingest.py      find the header row, map Power-BI column names, parse the fixed-width part description
   ▼
data/work/*.parquet
   │ labeling.py    1. classify every part number   2. give every segment a job set + symptom
   │ stats.py       hour percentiles, parts kits, symptom→fix tables
   ▼
data/quote.duckdb ──► engine.py ──► api/main.py (FastAPI) ──► quote page
```

**1. Part taxonomy** (`config/component_rules.csv`). The ERP description is truncated to 8 characters, for example `TY25879B        WET CHARCY`. Rules map these descriptions to a role:
`primary` (identifies the repair: battery, starter, alternator …), `consumable` (filters, fluids, hardware), `core`, `fee`, `hardware` or `other`.
Ambiguous descriptions are **checked against the text of the work orders they appear on**. For example, `ENGINE C` can mean *engine cranking motor* or *engine control unit*. A part that mostly appears on "PROGRAM NEW ECU" jobs becomes a controller, not a starter. Parts with no rule are promoted to primary when ≥ 60 % of their segments name one repair. `CR…` part numbers are recognised as core returns.

**2. Segment labelling** (`config/jobs.csv`, `config/symptoms.csv`). A segment's job set is the union of:
* the components of its primary parts, e.g. *"MACHINE NOT STARTING"* with a battery on it becomes `BATTERY`;
* components named in the text **with an action verb** ("REPLACE BATTERY" counts; "BATTERY DEAD" is a symptom). This also catches customer-supplied parts;
* service jobs from the text: PM with interval (`PM_500` …), PDI, travel, transport, wash, inspection, diagnose.

Diagnose and inspection are dropped when the real repair is present. Segments with no recognisable job stay unlabelled and are not used for hours.

**Context match** (`textsearch.py`, `config/synonyms.csv`). Every past segment description with usable hours is indexed. Labelled and unlabelled segments are both included, so jobs outside the job list can be quoted too. Two signals are combined, both learned from your own work-order text, with no external model or internet needed:
* TF-IDF on normalised words and word pairs, for wording. It expands abbreviations (R&R, ADJ, HYD, A/C), applies light stemming (ADJUSTMENT → ADJUST) and maps French terms (AJUSTEMENT, REMPLACER, FUITE);
* LSA, a 128-dimension SVD of the same matrix, for meaning from co-occurrence.

`config/synonyms.csv` maps trade phrases to one concept, e.g. valve lash / valve set / valve clearance → `VALVEADJUST`. Add a line whenever two wordings should count as the same job, then re-run `make pipeline`.

A query takes the tightest similarity cut that still leaves ≥ 40 past segments. Hours are weighted by how close each description is, and then blended model → base → make+division → division → all, exactly like job-list quotes.

**3. Statistics.** Only invoiced segments are used, Expense jobs are excluded, and hour outliers are removed with a robust z-score on log hours per job. Percentiles are computed at 5 levels: model → base model (333G → 333) → make+division → division → all. A quote **blends** the levels, `est = (n·level + k·parent)/(n + k)` with k = 8, so 4 battery jobs on a rare model are pulled toward what similar machines take.

Combined jobs come from history when the combination has ≥ 10 cases. Otherwise the per-job hours are added and multiplied by an overlap factor learned from multi-repair segments (shared access time).

**Parts kits** come from single-job segments. A part used on ≥ 60 % of the jobs goes in the kit; 20–60 % is listed as "sometimes needed". The part that *is* the job (the starter itself) is always in the kit, even when history is split across reman/new part numbers.

---

## Accuracy (backtest)

`make backtest` learns from segments invoiced before the cutoff (default 2026-01-01), quotes every later single-job segment, and writes `data/backtest_report.md`. The report covers:

* MAE, median absolute error, and the share of quotes within ±25 % / ±50 % of actual, for the engine and for a naive "global median per job" baseline;
* how often actual hours came in at or under the P80 (it should be about 80 %);
* a comparison with the **EQ hours** on the same segments, overall and only where EQ ≠ actual. If `EQ hours exactly equal the actual hours` on a large share of rows, the EQ field is being filled in after the work. It is then an optimistic ceiling, not a fair pre-job benchmark;
* per-job error, which shows where the rules or data need work;
* with `--similar-sample N` (`make backtest` uses 1500): job-list quotes and context quotes compared on the same sampled later segments, plus context quotes on segments that have no job code.

Per-segment hours are noisy because scope varies inside a job like "replace hose". The next gain needs richer scope inputs (machine hours, which hose or cylinder) or a model trained on description text.

## Tuning without code

| File | What to change |
|---|---|
| `config/component_rules.csv` | Part description patterns → role/component. First match wins. `verify=1` checks the rule against WO text. |
| `config/part_overrides.csv` | Force a specific part number, e.g. `RE12345,primary,STARTER,reman starter` |
| `config/jobs.csv` | Job list, display names, text patterns (EN/FR), exclusions, search synonyms |
| `config/synonyms.csv` | Phrases that mean the same job for the context match (valve lash = valve set = valve clearance) |
| `config/symptoms.csv` | Symptom patterns (NO_START, HYD_LEAK, …) |
| env vars (`QS_*`, see `src/quote_system/config.py`) | Thresholds: shrinkage k, kit attach rates, outlier cut-off, excluded sales types |

The pipeline writes `part_taxonomy` (with `source` = rule / rule_verified / text_vote / override) into the database. You can review it with:

```bash
python -c "import duckdb; print(duckdb.connect('data/quote.duckdb', read_only=True).sql(\"select * from part_taxonomy where source='text_vote' order by n_segments desc limit 30\"))"
```

## Project layout

```
config/                  editable rules (CSV)
src/quote_system/
  ingest.py              Excel/CSV → parquet (header detection, column aliases)
  labeling.py            part taxonomy + segment job labelling
  stats.py               labour stats, kits, symptom tables
  pipeline.py            orchestrates the above → data/quote.duckdb
  textsearch.py          context match over past descriptions (TF-IDF + LSA, synonyms)
  engine.py              quote logic shared by API and backtest
  backtest.py            time-split evaluation
  sample_data.py         synthetic exports in the real layout (tests / demo)
  api/main.py, store.py  FastAPI app + quote log
  api/static/index.html  quoting page
tests/                   pytest (runs on synthetic data, also in GitHub Actions)
.devcontainer/           Codespaces config
```

**Expected input columns** (Power-BI names or plain names both work):
work orders: `ServiceOrderID, Wo Number, Wo Segment, Make, Model, Description, Actual labour hours, EQ Labor Hours, Contract Type, Sales Type, Status, Invoice Date, Branch Name, Meter, ShopOffsite`.
Parts: `ServiceOrderID, Product Number, Description, Ship Qty`.

**Limitations:** there is no part pricing in the exports, so kits give part numbers and quantities only (join a price list later). Labels come from rules, so jobs the rules don't know stay unlabelled. Extend `jobs.csv` / `component_rules.csv` as gaps show up.
