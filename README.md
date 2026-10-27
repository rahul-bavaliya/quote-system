# Service quote system

Quote labour hours and a parts kit for a job (e.g. **Battery R&R on a JD 333G**) from historical work-order segments and the parts used on them.

* **Python + DuckDB** offline pipeline: Excel/CSV exports → part taxonomy → labelled segments → statistics + context-search index (about 2 minutes for 1M segments / 2.6M part lines).
* **FastAPI** service with a quoting page at `/` and Swagger docs at `/docs`.
* Runs in **GitHub Codespaces** with no extra services. The statistics database is a single DuckDB file.
* **Production:** Docker image (non-root, health check, pinned dependencies), API keys, JSON logs with request ids, input limits, data validation that refuses bad exports, and a data-quality report for every build. See [Production](#production).

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
  -d '{"make":"John Deere","model":"333 G","job_text":"replace battery"}'
```

```jsonc
{
  "jobs": ["BATTERY"],
  "machine": {"make": "JD", "model": "333G", "series": "333G", "base_model": "333", "make_entered": "John Deere", …},
  "total":  {"p25_hours": …, "p50_hours": …, "p80_hours": …, "p90_hours": …, "method": "…", "location": "ALL"},
  "by_location": {"SHOP":    {"p50_hours": …, "p80_hours": …, "sample_size": …, "confidence": "high"},
                  "OFFSITE": {"p50_hours": …, "p80_hours": …, "sample_size": …, "confidence": "medium"}},
  "labor":  [{"display_name": "Battery R&R", "level_used": "model", "sample_size": …, "confidence": "high",
              "basis": [{"level": "model", "key": "JD|333G", "n": …}, {"level": "base", "key": "JD|333"}, …]}],
  "parts":  [{"job_code": "BATTERY", "kit": [{"part_number": "…", "qty": 1, "attach_rate": …}], "optional": […]}],
  "similar_work_orders": [ …5 most recent battery jobs on this model… ],
  "quote_id": "…", "warnings": []
}
```

* **P50** is the typical job. **P80** is a safe quote: in backtests about 80 % of real jobs came in at or under it.
* **Shop vs off-site.** Every quote includes `by_location` with separate hours for shop and off-site (field) work, from the ShopOffsite field of past segments. Send `"location": "shop"` or `"offsite"` to make that location the headline number. Off-site travel is a separate job (`TRAVEL`); quote it as well if the call needs it.
* **Similar machines.** When a model has little or no history for the job, its learned similar machines are used before the broad levels, and broad history is scaled to the machine class. See [Similar machines](#similar-machines).
* **Make and model can be written any way.** "John Deere", "Deere", "JDE" → `JD`; "135G LC", "135G-LC" → `135GLC`. `GET /machines/resolve?make=…&model=…` shows how an entry is read. See [Messy makes and models](#messy-makes-and-models).
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
| GET | `/machines/resolve?make=John Deere&model=135G LC` | How a make/model entry is standardised |
| GET | `/health` (liveness) · `/ready` (data loaded) · `/version` · `/meta` · `/accuracy` | Operations |
| POST | `/admin/reload` (header `X-Admin-Key`) | Reload after re-running the pipeline |

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

**3. Statistics.** Only invoiced segments are used, Expense jobs and future invoice dates are excluded, and hour outliers are removed with a robust z-score on log hours per job. Percentiles are computed overall and separately for shop and off-site, at: model (135GLC) → series (135G) → base model (135) → similar machines → make+division → division → all. A quote **blends** the levels, `est = (n·level + k·parent)/(n + k)` with k = 8, so 4 battery jobs on a rare model are pulled toward what similar machines take.

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

## Messy makes and models

The exports contain the same brand under many spellings: JD, JDE, DEERE, JOHN, "JD #2", and names cut to 5 letters (HITAC, DOOSA, TOYOT). Models are also written several ways: "470G LC", "470GLC", "544 P". `normalize.py` handles this in four layers, during the pipeline and on every request:

1. **Deterministic clean-up.** Upper-case, accents and punctuation removed, unit numbers ("#2") dropped. The model key keeps only letters and digits, so `470G LC` = `470GLC` = `470g-lc`.
2. **Your aliases.** `config/make_aliases.csv` (`JOHN DEERE,JD`) and `config/model_aliases.csv` (`JD,135G LC,135GLC`).
3. **Automatic merges, only with evidence.** A rare spelling is folded into a big make only when its machines are that make's models. That means ≥ 50 % model overlap plus a prefix or near-spelling relationship (JDE → JD, HITAC → HIT), or ≥ 80 % overlap for small spellings. Notes typed into the make field on JD models ("SALES", "EXP", "2024") end up under JD this way. Short unrelated codes are left alone.
4. **Review.** Each build writes `data/review/make_mapping.csv`, listing every merged spelling with its method, segment count and model overlap. It also writes `model_variants.csv`, listing every model written more than one way. If a decision is wrong, add a line to the alias files and rebuild.

Variants are not forced together. **135G and 135GLC stay separate models in the same series (135G)**, so a 135GLC quote uses 135GLC history first and borrows from 135G and then the 135 family only as needed. Requests go through the same rules: "Hitachi" → HIT by prefix, and an unknown model like "135GX" falls back to its series/base. The response shows what was entered and how it was read.

## Similar machines

The exports have no machine-type or size field, so the pipeline learns which models are alike (`neighbors.py`):

* **Parts profile.** Models that use the same part numbers share a platform and size class.
* **Work profile.** Models with the same kinds of work are the same machine type: stick, swing, thumb and tracks mean excavator.

similarity = 0.6 × parts + 0.4 × work, within a make. On your data the 50G's neighbours are **60G, 35G, 50P, 30G, 60P, 35P, 26G, 50D**, all compact excavators. Every model's list is in `data/review/similar_machines.csv`. To pin a group, add it to `config/machine_types.csv`; it ships with JD compact excavators. A model with a pinned type only gets neighbours of the same type.

How a quote climbs: **model → series → base → similar machines → make + division → division → all**. Two rules keep big machines from setting the price for small ones:

1. **Similar machines come before the broad levels.** A 50G arm-cylinder job uses 50G history, then its siblings, and only then everything else.
2. **Broad history is scaled to the machine class.** When the broad levels are needed, they are first multiplied by a class factor. The factor is measured on jobs the class shares with the broad group. The job being quoted counts most (for cylinder work, 3.6 h on 50G-class machines vs 6.5 h on JD C&F), shrunk toward the median over all shared job types. For example, JD 50G arm cylinder replace has no 50G-class history; it comes out at **2.5 h** (31 jobs on other C&F machines × 0.57) instead of 6.7 h. Busy models with their own history are hardly affected.

Backtested on 2026 jobs, scaling **down** made quotes more accurate where it applied: within ±25 % rose from 24.5 % to 27.6 % of jobs, and the median error fell from 1.71 h to 1.64 h. Scaling **up** for big machines made them slightly worse, so the factor is capped at 1.0 by default (`QS_CLASS_FACTOR_MAX`).

The response lists `machine.similar_machines`, and each labour line has `similar_machines_used` and `scaled_to_machine_class`. The page shows both, so the estimator can see where the hours came from.

## Production

**Run with Docker** (any Linux host or VM; nothing else to install):

```bash
cp .env.example .env            # set QS_API_KEYS and QS_ADMIN_KEY (long random values)
mkdir -p data/raw && cp /path/to/workorders.xlsx /path/to/parts.xlsx data/raw/
sudo chown -R 10001:10001 data  # the container runs as non-root user 10001
docker compose build
docker compose run --rm pipeline          # validate + build statistics (2-3 min)
docker compose up -d api                  # http://<host>:8000  (put HTTPS in front, see below)
```

**Refresh with a new export:** copy the new files into `data/raw/`, run `docker compose run --rm pipeline`, then `curl -X POST -H "X-Admin-Key: …" http://<host>:8000/admin/reload`. You can also restart the API instead of calling reload. The new database is swapped in atomically, so a running API never sees a half-built one.

**Bad exports are refused.** The build stops and the live database stays unchanged when:
* there are too few segments or part lines;
* models or hours are mostly empty;
* fewer than half the part orders match a work order (files from different periods);
* segments drop more than 30 % versus the previous build.

Use `--force` only when the change is intended. Every build writes `data/review/data_quality.md` and `build_manifest.json`. These record input file hashes, volumes, what was excluded and why, the make/model merges and the labelling counts.

| Area | What is in place |
|---|---|
| Security | API keys (`X-API-Key`, constant-time compare) for every API call; separate admin key for reload; non-root container; no company data in the image (data is a volume; `.dockerignore`/`.gitignore` block exports); input size limits on every field; CORS off unless `QS_CORS_ORIGINS` is set |
| Observability | JSON logs (`QS_LOG_JSON=1`): one line per request with request id, path, status, latency; `X-Request-ID` echoed back; unhandled errors logged with stack trace, client gets `{"detail":"internal error","request_id":…}` |
| Health | `/health` liveness (container `HEALTHCHECK`), `/ready` returns 503 until statistics are loaded (use for load balancers), `/version` |
| Reliability | pinned dependencies (`requirements.lock`); quote log in SQLite WAL mode (safe for several workers); atomic database swap; reload without restart |
| Quality gates | CI runs ruff and 31 tests, then builds the image and smoke-tests it: pipeline on synthetic data, auth required, a real quote, admin reload |
| Sizing | about 1 GB RAM per API worker (`QS_WORKERS`, default 2) and ~2.5 GB during a build; quotes take 0.05–0.3 s |

**Put HTTPS in front.** Use the company reverse proxy, IIS/ARR, nginx, an Azure App Gateway or similar, and forward to port 8000. The app trusts `X-Forwarded-*` headers from the proxy. For SSO, let the proxy authenticate users, and keep API keys for system-to-system calls.

**Scaling beyond one server.** The statistics file is read-only, so any number of API instances can share a copy. The quote log (`quotes.sqlite`) is the one piece of writable state. When several servers must share it, move it to Postgres.

## Tuning without code

| File | What to change |
|---|---|
| `config/component_rules.csv` | Part description patterns → role/component. First match wins. `verify=1` checks the rule against WO text. |
| `config/part_overrides.csv` | Force a specific part number, e.g. `RE12345,primary,STARTER,reman starter` |
| `config/jobs.csv` | Job list, display names, text patterns (EN/FR), exclusions, search synonyms |
| `config/synonyms.csv` | Phrases that mean the same job for the context match (valve lash = valve set = valve clearance) |
| `config/symptoms.csv` | Symptom patterns (NO_START, HYD_LEAK, …) |
| `config/make_aliases.csv` · `config/model_aliases.csv` | Make / model spellings that mean the same machine |
| `config/machine_types.csv` | Pin models to a machine type (e.g. JD compact excavators); similar machines stay within the type |
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
  validate.py            refuse bad exports, data-quality report, build manifest
  normalize.py           make / model standardisation (aliases, evidence-based merges, series level)
  neighbors.py           similar machines learned from parts + work history
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
