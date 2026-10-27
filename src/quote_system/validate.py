"""Data validation for each build: a bad export must never replace good statistics.

Hard checks (the build stops, the running API keeps the previous database):
  * enough work-order segments and part lines
  * required columns mostly filled (service order id, model, actual hours)
  * part lines actually join to work orders
  * no sudden collapse versus the previous build (e.g. someone exported one month only)
Soft checks are reported in data/review/data_quality.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)


class ValidationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_manifest(raw_dir: Path) -> list[dict]:
    out = []
    for p in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
        if p.is_file() and p.suffix.lower() in {".xlsx", ".xlsm", ".xls", ".csv", ".txt"}:
            out.append({"file": p.name, "bytes": p.stat().st_size, "sha256": _sha256(p)})
    return out


def previous_segments(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        row = con.execute("select v from meta where k = 'segments'").fetchone()
        con.close()
        return int(json.loads(row[0])) if row else None
    except Exception:  # noqa: BLE001 - an unreadable old db just means no comparison
        return None


def validate(wo_parquet: Path, parts_parquet: Path, settings, previous: int | None, force: bool = False) -> dict:
    con = duckdb.connect()
    w, p = f"'{wo_parquet}'", f"'{parts_parquet}'"
    today = date.today().isoformat()
    q = lambda sql: con.execute(sql).fetchone()  # noqa: E731
    seg, ids, no_model, no_hours, neg_hours, future_inv, no_inv, huge = q(f"""
        select count(*), count(distinct service_order_id),
               count(*) filter (where model = 'UNKNOWN'),
               count(*) filter (where actual_hours is null),
               count(*) filter (where actual_hours < 0),
               count(*) filter (where invoice_date > date '{today}' + interval 1 day),
               count(*) filter (where invoice_date is null),
               count(*) filter (where actual_hours > {settings.max_hours})
        from {w}""")
    lines, line_ids, joined_ids = q(f"""
        select count(*), count(distinct service_order_id),
               count(distinct service_order_id) filter (where service_order_id in (select service_order_id from {w}))
        from {p}""")
    d_from, d_to = q(f"select min(invoice_date), max(invoice_date) from {w} where invoice_date <= date '{today}'")
    report = {
        "segments": seg, "distinct_service_orders": ids, "part_lines": lines,
        "part_service_orders": line_ids, "part_service_orders_matched": joined_ids,
        "part_match_rate": round(joined_ids / line_ids, 4) if line_ids else 0.0,
        "model_missing_share": round(no_model / seg, 4) if seg else 1.0,
        "actual_hours_missing_share": round(no_hours / seg, 4) if seg else 1.0,
        "negative_hours": neg_hours, "hours_above_max": huge,
        "future_invoice_dates": future_inv, "not_invoiced": no_inv,
        "invoice_date_range": [str(d_from), str(d_to)], "previous_build_segments": previous,
    }
    problems = []
    if seg < settings.min_segments:
        problems.append(f"only {seg} work-order segments (minimum {settings.min_segments})")
    if lines < settings.min_part_lines:
        problems.append(f"only {lines} part lines (minimum {settings.min_part_lines})")
    if report["model_missing_share"] > 0.5:
        problems.append(f"{report['model_missing_share']:.0%} of segments have no model")
    if report["actual_hours_missing_share"] > 0.5:
        problems.append(f"{report['actual_hours_missing_share']:.0%} of segments have no actual hours")
    if line_ids and report["part_match_rate"] < 0.5:
        problems.append(f"only {report['part_match_rate']:.0%} of part service orders match a work order "
                        "(were the two files exported for the same period?)")
    if previous and seg < (1 - settings.max_drop) * previous:
        problems.append(f"segments dropped from {previous} to {seg} (more than {settings.max_drop:.0%}); "
                        "if this is intended run the pipeline with --force")
    report["problems"] = problems
    if problems and not force:
        raise ValidationError("data validation failed:\n  - " + "\n  - ".join(problems))
    if problems:
        log.warning("validation problems ignored because of --force: %s", problems)
    return report


def write_quality_report(path: Path, validation: dict, standardisation: dict, labelling: dict | None,
                         manifest: dict) -> None:
    v, s = validation, standardisation
    lines = [
        "# Data quality report", "",
        f"Built: {manifest.get('built_at')} · version {manifest.get('version')}", "",
        "## Inputs", "", "| File | Size | SHA-256 |", "|---|---|---|",
        *[f"| {f['file']} | {f['bytes']:,} | `{f['sha256'][:16]}…` |" for f in manifest.get("inputs", [])], "",
        "## Volumes", "",
        f"* Work-order segments: **{v['segments']:,}** (previous build: {v['previous_build_segments'] or 'n/a'})",
        f"* Part lines: **{v['part_lines']:,}**; part service orders matching a work order: "
        f"{v['part_match_rate']:.1%}",
        f"* Invoice dates: {v['invoice_date_range'][0]} → {v['invoice_date_range'][1]}", "",
        "## Cleaned or excluded", "",
        f"* Model missing: {v['model_missing_share']:.1%} of segments",
        f"* Actual hours missing: {v['actual_hours_missing_share']:.1%}; negative: {v['negative_hours']:,}; "
        f"above the {manifest.get('max_hours')} h cap: {v['hours_above_max']:,} (excluded from hours)",
        f"* Invoice date in the future: {v['future_invoice_dates']:,} (excluded); not invoiced: {v['not_invoiced']:,}",
        "",
        "## Make / model standardisation", "",
        f"* Make spellings: {s['makes_as_written']:,} → {s['makes_canonical']:,} makes "
        f"({s['make_spellings_merged']:,} spellings merged, covering {s['segments_with_merged_make']:,} segments)",
        f"* Make/model spellings: {s['models_as_written']:,} → {s['models_canonical']:,} models",
        "* Review every decision in `make_mapping.csv` and `model_variants.csv` (same folder). To fix one, add a "
        "line to `config/make_aliases.csv` or `config/model_aliases.csv` and rebuild.", "",
    ]
    if labelling:
        lines += ["## Job labelling", "",
                  *[f"* {k.replace('_', ' ')}: {val:,}" for k, val in labelling.items()], ""]
    if v.get("problems"):
        lines += ["## Problems (build forced)", "", *[f"* {p_}" for p_ in v["problems"]], ""]
    path.write_text("\n".join(lines))
