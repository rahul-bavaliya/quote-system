"""Offline pipeline: raw exports -> data/quote.duckdb (the only thing the API needs).

    python -m quote_system.pipeline            # full run
    python -m quote_system.pipeline --skip-ingest   # reuse data/work/*.parquet
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from pathlib import Path

import duckdb

from .config import Settings, get_settings
from .ingest import ingest
from .labeling import classify_parts, label_segments
from .logging_setup import setup_logging
from .neighbors import build_neighbors
from .normalize import standardize
from .rules import load_jobs, pm_job_codes
from .stats import LOCATION_SQL, build_stats
from .textsearch import build_index, index_dir_for
from .validate import ValidationError, input_manifest, previous_segments, validate, write_quality_report

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


def build_database(settings: Settings, db_path: Path, cutoff: str | None = None, skip_ingest: bool = False,
                   force: bool = False) -> dict:
    t0 = time.time()
    settings.ensure_dirs()
    wo = settings.work_dir / "workorders.parquet"
    parts = settings.work_dir / "parts.parquet"
    if not skip_ingest or not (wo.exists() and parts.exists()):
        ingest(settings)
    log.info("ingest done in %.1fs", time.time() - t0)
    # a bad export must not replace good statistics: this raises before anything is written
    validation = validate(wo, parts, settings, previous_segments(db_path), force=force)
    log.info("validation: %s", {k: validation[k] for k in ("segments", "part_lines", "part_match_rate")})

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
    norm_stats = standardize(con, settings)
    log.info("make/model standardisation: %s", norm_stats)

    classify_parts(con, settings)
    log.info("taxonomy done in %.1fs", time.time() - t0)
    labelling = label_segments(con, settings)
    log.info("labelling done in %.1fs", time.time() - t0)
    meta = build_stats(con, settings, cutoff=cutoff)
    meta["similar_machines"] = build_neighbors(con, settings)
    meta["standardisation"] = norm_stats
    meta["validation"] = validation
    manifest = {"built_at": meta["built_at"], "version": settings.version, "cutoff": cutoff,
                "inputs": input_manifest(settings.raw_dir) if not cutoff else [], "max_hours": settings.max_hours,
                "standardisation": norm_stats, "labelling": labelling, "validation": validation}
    for k, v in (("similar_machines", meta["similar_machines"]), ("standardisation", norm_stats),
                 ("validation", validation), ("labelling", labelling),
                 ("manifest", manifest)):
        con.execute("insert into meta values (?, ?)", [k, json.dumps(v, default=str)])
    log.info("stats done in %.1fs", time.time() - t0)

    # context search: every segment with usable hours, labelled or not ('VALVE LASH ADJUSTMENT' has no job code)
    con.execute(f"""
        create or replace table text_pool as
        select service_order_id, wo_number, wo_segment, branch, invoice_date, description, norm_text,
               make, model, base_model, division, actual_hours, eq_hours, job_key, series, {LOCATION_SQL} as location,
               model_raw
        from scoped where actual_hours > 0 and actual_hours <= {settings.max_hours} and norm_text <> ''
    """)
    con.execute("create index if not exists ix_pool_text on text_pool (norm_text)")
    # parts-based matching: how much each part number tells you about the job (idf) and per-segment norms
    con.execute("""
        create or replace temp table seg_parts as
        select distinct p.service_order_id, p.part_number
        from parts p join text_pool t using (service_order_id) join part_taxonomy x using (part_number)
        where x.role <> 'fee';
        create or replace table part_idf as
        with n as (select count(distinct service_order_id) as total from seg_parts)
        select part_number, regexp_replace(part_number, '[^A-Z0-9]', '', 'g') as pn_key, count(*) as segs,
               ln(any_value(n.total) / count(*)) + 1 as idf
        from seg_parts, n group by part_number;
        create or replace table seg_part_norm as
        select s.service_order_id, sqrt(sum(i.idf * i.idf)) as norm, count(*) as n_parts
        from seg_parts s join part_idf i using (part_number) group by 1;
        create index if not exists ix_parts_pn on parts (part_number);
    """)
    texts = con.execute("select norm_text, count(*) from text_pool group by 1 order by 1").fetchall()
    idx_tmp = index_dir_for(db_path).with_name(index_dir_for(db_path).name + ".building")
    shutil.rmtree(idx_tmp, ignore_errors=True)
    meta["text_index"] = build_index([t for t, _ in texts], [n for _, n in texts], idx_tmp)
    con.execute("insert into meta values ('text_index', ?)", [json.dumps(meta["text_index"])])
    log.info("text index %s, done in %.1fs", meta["text_index"], time.time() - t0)

    # keep the serving db lean: raw tables are in parquet already
    for t in ("workorders", "eligible", "scoped", "hours_sample", "part_desc", "part_rule"):
        con.execute(f"drop table if exists {t}")
    con.execute("create index if not exists ix_seg_job on segments (job_key, make, model)")
    con.execute("checkpoint")
    con.close()
    shutil.move(str(tmp), str(db_path))  # atomic swap so a running API never sees a half-built db
    final_idx = index_dir_for(db_path)
    shutil.rmtree(final_idx, ignore_errors=True)
    shutil.move(str(idx_tmp), str(final_idx))
    if not cutoff:  # the live build documents itself; backtest builds do not overwrite the review files
        review = db_path.parent / "review"
        review.mkdir(parents=True, exist_ok=True)
        manifest["seconds"] = round(time.time() - t0, 1)
        (review / "build_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
        write_quality_report(review / "data_quality.md", validation, norm_stats, labelling, manifest)
    log.info("wrote %s in %.1fs", db_path, time.time() - t0)
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-ingest", action="store_true", help="reuse data/work/*.parquet from a previous run")
    ap.add_argument("--cutoff", help="only learn from segments invoiced before YYYY-MM-DD")
    ap.add_argument("--db", help="output database path (default data/quote.duckdb)")
    ap.add_argument("--force", action="store_true", help="build even if data validation finds problems")
    args = ap.parse_args()
    s = get_settings()
    setup_logging(s.log_json, s.log_level)
    try:
        meta = build_database(s, Path(args.db) if args.db else s.db_path, cutoff=args.cutoff,
                              skip_ingest=args.skip_ingest, force=args.force)
    except ValidationError as e:
        log.error(str(e))
        print(f"\nBuild stopped - the previous database (if any) is unchanged.\n{e}\n")
        raise SystemExit(2) from None
    print("\nBuild complete:")
    for k in ("segments", "eligible_segments", "hours_sample", "single_job_sample", "data_from", "data_to",
              "combo_overlap_factor"):
        print(f"  {k:22s} {meta[k]}")
    print(f"  {'review files':22s} {(Path(args.db) if args.db else s.db_path).parent / 'review'}")


if __name__ == "__main__":
    main()
