"""Backtest: learn from segments invoiced before a cutoff, quote every later single-job segment,
and compare with the actual hours - and with the EQ hours that were entered at the time.

    python -m quote_system.backtest --cutoff 2026-01-01
"""
from __future__ import annotations

import argparse
import logging
import statistics
from collections import defaultdict
from pathlib import Path

from .config import get_settings
from .engine import QuoteEngine
from .pipeline import build_database


def _metrics(rows: list[tuple[float, float]]) -> dict:
    if not rows:
        return {}
    err = [abs(p - a) for p, a in rows]
    ape = [abs(p - a) / a for p, a in rows if a > 0]
    return {
        "n": len(rows),
        "mae_hours": round(statistics.fmean(err), 2),
        "median_abs_err_hours": round(statistics.median(err), 2),
        # jobs with 0 actual hours have no percentage error; None when every row is like that
        "within_25pct": round(sum(x <= 0.25 for x in ape) / len(ape), 3) if ape else None,
        "within_50pct": round(sum(x <= 0.50 for x in ape) / len(ape), 3) if ape else None,
    }


def run(cutoff: str, rebuild: bool = True, max_rows: int | None = None) -> dict:
    s = get_settings()
    db = s.db_path.with_name("backtest.duckdb")
    if rebuild or not db.exists():
        build_database(s, db, cutoff=cutoff, skip_ingest=True)
    eng = QuoteEngine(s, db)
    excl = tuple(x.strip() for x in s.stats_exclude_sales_types.split(",") if x.strip()) or ("",)
    rows = eng.cursor().execute(f"""
        select job_key, make, model, actual_hours, eq_hours from segments
        where status = 'Invoiced' and invoice_date >= date '{cutoff}' and n_jobs = 1
          and actual_hours > 0 and actual_hours <= {s.max_hours}
          and coalesce(sales_type, '') not in ({", ".join("?" for _ in excl)})
        {f"limit {int(max_rows)}" if max_rows else ""}
    """, list(excl)).fetchall()

    machines: dict = {}
    model_rows, base_rows, eq_pairs, cover = [], [], [], []
    per_job = defaultdict(list)
    for job, make, model, act, eq in rows:
        m = machines.get((make, model)) or machines.setdefault((make, model), eng.machine(make, model))
        est = eng.labor_estimate(job, m)
        g = eng.stats.get((job, "global", ""))
        if not est or not g:
            continue
        p50 = est["p50_hours"]
        model_rows.append((p50, act))
        base_rows.append((g["p50"], act))
        per_job[job].append((p50, act))
        cover.append(act <= est["p80_hours"])
        if eq and eq > 0:
            eq_pairs.append((p50, eq, act))

    report = {
        "cutoff": cutoff,
        "test_segments": len(rows), "quoted": len(model_rows),
        "engine_p50": _metrics(model_rows),
        "baseline_global_median": _metrics(base_rows),
        "share_actual_at_or_below_p80": round(sum(cover) / len(cover), 3) if cover else None,
        "vs_eq_hours": {
            "rows_with_eq": len(eq_pairs),
            "engine_p50": _metrics([(p, a) for p, _, a in eq_pairs]),
            "eq_hours": _metrics([(e, a) for _, e, a in eq_pairs]),
            "share_eq_equal_actual": round(sum(abs(e - a) < 0.01 for _, e, a in eq_pairs) / len(eq_pairs), 3) if eq_pairs else None,
            "engine_p50_eq_differs": _metrics([(p, a) for p, e, a in eq_pairs if abs(e - a) >= 0.01]),
            "eq_hours_eq_differs": _metrics([(e, a) for _, e, a in eq_pairs if abs(e - a) >= 0.01]),
        },
        "per_job": {j: _metrics(v) for j, v in sorted(per_job.items(), key=lambda x: -len(x[1]))[:25]},
    }
    out = s.db_path.with_name("backtest_report.md")
    out.write_text(_markdown(report))
    report["report_path"] = str(out)
    return report


def run_similar(cutoff: str, sample: int = 1500, seed: int = 0) -> dict:
    """Compare job-code quotes with context (similar-description) quotes on the same later segments."""
    import random

    s = get_settings()
    eng = QuoteEngine(s, s.db_path.with_name("backtest.duckdb"))
    excl = tuple(x.strip() for x in s.stats_exclude_sales_types.split(",") if x.strip()) or ("",)
    base_sql = f"""
        select job_key, n_jobs, make, model, description, actual_hours from segments
        where status = 'Invoiced' and invoice_date >= date '{cutoff}' and actual_hours > 0
          and actual_hours <= {s.max_hours} and coalesce(sales_type, '') not in ({", ".join("?" for _ in excl)})
          and norm_text <> ''"""
    rows = eng.cursor().execute(base_sql, list(excl)).fetchall()
    rnd = random.Random(seed)
    labelled = [r for r in rows if r[1] == 1]
    unlabelled = [r for r in rows if r[1] == 0]
    labelled = rnd.sample(labelled, min(sample, len(labelled)))
    unlabelled = rnd.sample(unlabelled, min(sample // 2, len(unlabelled)))
    cat, sim, sim_unl = [], [], []
    for job, _, make, model, desc, act in labelled:
        m = eng.machine(make, model)
        a = eng.labor_estimate(job, m)
        b = eng.similar_quote(desc, m, include_parts=False)
        if a and b:
            cat.append((a["p50_hours"], act))
            sim.append((b["total"]["p50_hours"], act))
    for _, _, make, model, desc, act in unlabelled:
        b = eng.similar_quote(desc, eng.machine(make, model), include_parts=False)
        if b:
            sim_unl.append((b["total"]["p50_hours"], act))
    return {"labelled_job_code": _metrics(cat), "labelled_similar": _metrics(sim),
            "unlabelled_similar": _metrics(sim_unl), "unlabelled_tried": len(unlabelled)}


def _markdown(r: dict) -> str:
    def row(name, m):
        return (f"| {name} | {m.get('n', 0)} | {m.get('mae_hours', '-')} | {m.get('median_abs_err_hours', '-')} | "
                f"{m.get('within_25pct', '-')} | {m.get('within_50pct', '-')} |")
    hdr = "| | n | MAE h | median abs err h | within 25% | within 50% |\n|---|---|---|---|---|---|"
    lines = [f"# Backtest (trained before {r['cutoff']}, tested on later single-job segments)", "",
             f"Test segments: {r['test_segments']}, quoted: {r['quoted']}, "
             f"actual <= P80: {r['share_actual_at_or_below_p80']}", "", "## Overall", "", hdr,
             row("Engine P50 (model -> global blend)", r["engine_p50"]),
             row("Baseline: global median per job", r["baseline_global_median"]), "",
             f"## Against EQ hours (rows where EQ hours > 0: {r['vs_eq_hours']['rows_with_eq']})", "", hdr,
             row("Engine P50", r["vs_eq_hours"]["engine_p50"]), row("EQ hours", r["vs_eq_hours"]["eq_hours"]),
             row("Engine P50 (rows where EQ != actual)", r["vs_eq_hours"]["engine_p50_eq_differs"]),
             row("EQ hours (rows where EQ != actual)", r["vs_eq_hours"]["eq_hours_eq_differs"]), "",
             f"EQ hours exactly equal the actual hours on {r['vs_eq_hours']['share_eq_equal_actual']} of these rows, "
             "so the EQ field is often filled in or adjusted after the work - treat it as an optimistic benchmark, "
             "not as a pre-job quote.", "",
             "## Per job (engine P50)", "", hdr]
    lines += [row(j, m) for j, m in r["per_job"].items()]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2026-01-01")
    ap.add_argument("--no-rebuild", action="store_true")
    ap.add_argument("--similar-sample", type=int, default=0,
                    help="also compare job-code vs context quotes on N sampled later segments")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run(args.cutoff, rebuild=not args.no_rebuild)
    print(Path(r["report_path"]).read_text())
    if args.similar_sample:
        sim = run_similar(args.cutoff, args.similar_sample)
        lines = ["", "## Job-code vs context (similar description) quotes", "",
                 "| | n | MAE h | median abs err h | within 25% | within 50% |", "|---|---|---|---|---|---|"]
        for name, key in (("Job-code quote (labelled segments)", "labelled_job_code"),
                          ("Context quote (same segments)", "labelled_similar"),
                          ("Context quote (segments with no job code)", "unlabelled_similar")):
            m = sim[key]
            lines.append(f"| {name} | {m.get('n', 0)} | {m.get('mae_hours', '-')} | {m.get('median_abs_err_hours', '-')} | "
                         f"{m.get('within_25pct', '-')} | {m.get('within_50pct', '-')} |")
        text = "\n".join(lines) + "\n"
        with open(r["report_path"], "a") as f:
            f.write(text)
        print(text)


if __name__ == "__main__":
    main()
