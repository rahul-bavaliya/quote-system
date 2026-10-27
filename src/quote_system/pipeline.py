"""Offline pipeline: raw exports -> data/quote.duckdb (the only thing the API needs).

    python -m quote_system.pipeline            # full run
    python -m quote_system.pipeline --skip-ingest   # reuse data/work/*.parquet
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import time
from pathlib import Path

import duckdb

from .config import Settings, get_settings
from .ingest import ingest
from .labeling import classify_parts, label_segments
from .rules import load_jobs, pm_job_codes
from .stats import build_stats

log = logging.getLogger("quote_system.pipeline")


def _load_job_catalog(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    rows = [(j.job_code, j.display_name, j.category, ";".join(j.synonyms)) for j in load_jobs(settings.config_dir)
            if j.job_code != "PM_SERVICE"]
    pm = [j for j in load_jobs(settings.config_dir) if j.job_code == "PM_SERVICE"]
    if pm:
        rows.append(("PM_SERVICE", pm[0].display_name + " (interval not stated)", "service", ";".join(pm[0].synonyms)))
        rows += [(code, name, "service", f"{code[3:]} hour service;{code[3:]} hr service;{code[3:]}hr pm")
                 for code, name in pm_job_codes()]
    con.execute("create or replace table job_catalog (job_code varchar, display_name varchar, category varchar, synonyms varchar)")
    con.executemany("insert into job_catalog values (?, ?, ?, ?)", rows)


def build_database(settings: Settings, db_path: Path, cutoff: str | None = None, skip_ingest: bool = False) -> dict:
    t0 = time.time()
    settings.ensure_dirs()
    wo = settings.work_dir / "workorders.parquet"
    parts = settings.work_dir / "parts.parquet"
    if not skip_ingest or not (wo.exists() and parts.exists()):
        ingest(settings)
    log.info("ingest done in %.1fs", time.time() - t0)

    tmp = db_path.with_suffix(".building.duckdb")
    for p in (tmp, Path(str(tmp) + ".wal")):
        if p.exists():
            p.unlink()
    con = duckdb.connect(str(tmp))
    threads = os.environ.get("QS_THREADS")
    if threads:
        con.execute(f"set threads = {int(threads)}")
    con.execute(f"create table workorders as select * from '{wo}'")
    con.execute(f"create table parts as select * from '{parts}'")
    _load_job_catalog(con, settings)

    classify_parts(con, settings)
    log.info("taxonomy done in %.1fs", time.time() - t0)
    label_segments(con, settings)
    log.info("labelling done in %.1fs", time.time() - t0)
    meta = build_stats(con, settings, cutoff=cutoff)
    log.info("stats done in %.1fs", time.time() - t0)

    # keep the serving db lean: raw tables are in parquet already
    for t in ("workorders", "eligible", "scoped", "hours_sample", "part_desc", "part_rule"):
        con.execute(f"drop table if exists {t}")
    con.execute("create index if not exists ix_seg_job on segments (job_key, make, model)")
    con.execute("checkpoint")
    con.close()
    shutil.move(str(tmp), str(db_path))  # atomic swap so a running API never sees a half-built db
    log.info("wrote %s in %.1fs", db_path, time.time() - t0)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-ingest", action="store_true", help="reuse data/work/*.parquet from a previous run")
    ap.add_argument("--cutoff", help="only learn from segments invoiced before YYYY-MM-DD")
    ap.add_argument("--db", help="output database path (default data/quote.duckdb)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = get_settings()
    meta = build_database(s, Path(args.db) if args.db else s.db_path, cutoff=args.cutoff, skip_ingest=args.skip_ingest)
    print("\nBuild complete:")
    for k in ("segments", "eligible_segments", "hours_sample", "single_job_sample", "data_from", "data_to",
              "combo_overlap_factor"):
        print(f"  {k:22s} {meta[k]}")


if __name__ == "__main__":
    main()
