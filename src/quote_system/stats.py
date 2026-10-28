"""Step 3 - turn labelled segments into the serving tables the API reads.

Tables written:
  models          make/model -> base model, division, volume (for autocomplete + hierarchy)
  labor_stats     hour percentiles per job_key at 5 levels: model, base, make_div, division, global
  part_kits       attach rate + median qty of each part on single-job segments, per level
  symptom_jobs    for a symptom ('NO_START'), how often each repair was the actual fix
  meta            build info and global factors (e.g. combo overlap factor)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import duckdb

from .config import Settings

log = logging.getLogger(__name__)

# level name -> SQL expression for the grouping key (all levels are additionally grouped by job_key)
LEVELS = {
    "model": "make || '|' || model",
    "series": "make || '|' || series",
    "base": "make || '|' || base_model",
    "make_div": "make || '|' || division",
    "division": "division",
    "global": "''",
}
# shop vs field work: 'SHOP' / 'OFFSITE' (anything else is left out of the split, but kept in 'ALL')
LOCATION_SQL = ("case when upper(coalesce(location, '')) like 'SHOP%' then 'SHOP' "
                "when regexp_matches(upper(coalesce(location, '')), 'OFF|FIELD|SITE') then 'OFFSITE' else null end")

KIT_KEYS = {
    "model": "s.make || '|' || s.model",
    "series": "s.make || '|' || s.series",
    "base": "s.make || '|' || s.base_model",
    "make_div": "s.make || '|' || s.division",
}


def build_stats(con: duckdb.DuckDBPyConnection, settings: Settings, cutoff: str | None = None) -> dict:
    """``cutoff`` (YYYY-MM-DD) limits training data to segments invoiced before that date (backtesting)."""
    statuses = ", ".join("'" + s.strip() + "'" for s in settings.stats_statuses.split(",") if s.strip())
    date_filter = f"and invoice_date < date '{cutoff}'" if cutoff else ""
    excl = [x.strip() for x in settings.stats_exclude_sales_types.split(",") if x.strip()]
    if excl:
        date_filter += " and coalesce(sales_type, '') not in (" + ", ".join("'" + x + "'" for x in excl) + ")"

    con.execute(f"""
        create or replace table scoped as
        select * from segments where status in ({statuses}) and invoice_date is not null
          and invoice_date <= current_date + interval 1 day {date_filter};
        create or replace table eligible as select * from scoped where n_jobs > 0;
    """)
    # hours sample: positive, sane hours, robust outlier removal on log scale per job_key
    con.execute(f"""
        create or replace table hours_sample as
        with s as (
          select service_order_id, job_key, n_jobs, make, model, series, base_model, division, actual_hours, eq_hours,
                 invoice_date, ln(actual_hours) as lh, {LOCATION_SQL} as location
          from eligible where actual_hours > 0 and actual_hours <= {settings.max_hours}
        ),
        m as (select job_key, median(lh) as med from s group by 1),
        d as (select s.job_key, median(abs(s.lh - m.med)) as mad from s join m using (job_key) group by 1)
        select s.* exclude (lh), (s.lh - m.med) / greatest(1.4826 * d.mad, 0.1) as robust_z
        from s join m using (job_key) join d using (job_key)
    """)
    con.execute(f"create or replace table hours_clean as select * from hours_sample "
                f"where abs(robust_z) <= {settings.outlier_mad_z}")

    unions = " union all ".join(f"""
        select '{lvl}' as level, job_key, {expr} as key, coalesce(location, 'ALL') as location, count(*) as n,
               quantile_cont(actual_hours, 0.25) as p25, median(actual_hours) as p50,
               quantile_cont(actual_hours, 0.80) as p80, quantile_cont(actual_hours, 0.90) as p90,
               avg(actual_hours) as mean,
               count(*) filter (where eq_hours > 0) as eq_n,
               median(eq_hours) filter (where eq_hours > 0) as eq_p50,
               max(invoice_date) as last_date
        from hours_clean group by grouping sets ((job_key, {expr}), (job_key, {expr}, location))
        having location is not null or grouping(location) = 1""" for lvl, expr in LEVELS.items())
    con.execute(f"create or replace table labor_stats as {unions}")

    con.execute("""
        create or replace table models as
        with d as (select make, model, series, base_model, division, count(*) as n from segments
                   where model <> 'UNKNOWN' group by all)
        select d.make, d.model, min(d.series) as series, min(d.base_model) as base_model,
               first(d.division order by d.n desc, d.division) as division, sum(d.n) as n_segments,
               any_value(md.display_model) as display_model
        from d left join model_display md using (make, model) group by d.make, d.model
    """)

    # parts kits from single-job segments
    kit_unions = " union all ".join(f"""
        select '{lvl}' as level, s.job_key as job_code, {KIT_KEYS[lvl]} as key,
               s.service_order_id, p.part_number, p.qty
        from kit_segs s left join kit_parts p using (service_order_id)""" for lvl in KIT_KEYS)
    con.execute("""
        create or replace temp table kit_segs as
        select service_order_id, job_key, make, model, series, base_model, division from eligible where n_jobs = 1;
        create or replace temp table kit_parts as
        select service_order_id, part_number, sum(qty) as qty from parts
        where service_order_id in (select service_order_id from kit_segs) and coalesce(qty, 0) > 0
        group by all;
    """)
    con.execute(f"""
        create or replace table part_kits as
        with x as ({kit_unions}),
        tot as (select level, job_code, key, count(distinct service_order_id) as n_job_segments from x group by all),
        per as (select level, job_code, key, part_number, count(distinct service_order_id) as n_with_part,
                       median(qty) as median_qty
                from x where part_number is not null group by all)
        select per.level, per.job_code, per.key, per.part_number, t.description, t.role, t.component,
               tot.n_job_segments, per.n_with_part, per.n_with_part / tot.n_job_segments as attach_rate,
               per.median_qty
        from per join tot using (level, job_code, key)
        left join part_taxonomy t using (part_number)
        where per.n_with_part / tot.n_job_segments >= {settings.kit_optional_attach} and tot.n_job_segments >= 2
    """)

    con.execute(f"""
        create or replace table kit_levels as
        with x as ({kit_unions.replace(' left join kit_parts p using (service_order_id)', '').replace(', p.part_number, p.qty', '')})
        select level, job_code, key, count(distinct service_order_id) as n_job_segments from x group by all
    """)

    # symptom -> which repair actually fixed it (component jobs only)
    _symptom_jobs(con)

    # combo overlap: actual hours of multi-component segments vs the sum of their single-job global medians
    overlap = con.execute("""
        with comp as (select list(job_code) as codes from job_catalog where category = 'component'),
        single as (select job_key, p50 from labor_stats where level = 'global' and location = 'ALL' and job_key not like '%+%'),
        multi as (
          select h.service_order_id, h.actual_hours, unnest(string_split(h.job_key, '+')) as j
          from hours_clean h, comp
          where h.n_jobs > 1 and len(list_filter(string_split(h.job_key, '+'), lambda x: not list_contains(comp.codes, x))) = 0
        ),
        per as (
          select m.service_order_id, any_value(m.actual_hours) as act, sum(s.p50) as sum_single,
                 count(s.p50) as matched, count(*) as total
          from multi m left join single s on s.job_key = m.j group by 1
        )
        select median(act / sum_single), count(*) from per where matched = total and sum_single > 0
    """).fetchone()
    overlap_factor = float(overlap[0]) if overlap and overlap[0] else 1.0

    info = con.execute("""
        select (select count(*) from segments), (select count(*) from eligible), (select count(*) from hours_clean),
               (select count(*) from hours_clean where n_jobs = 1),
               (select min(invoice_date) from eligible), (select max(invoice_date) from eligible)
    """).fetchone()
    meta = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cutoff": cutoff,
        "segments": info[0], "eligible_segments": info[1], "hours_sample": info[2], "single_job_sample": info[3],
        "data_from": str(info[4]), "data_to": str(info[5]),
        "combo_overlap_factor": round(overlap_factor, 4),
        "settings": {k: (str(v) if not isinstance(v, (int, float, str)) else v) for k, v in settings.__dict__.items()},
    }
    con.execute("create or replace table meta (k varchar, v varchar)")
    con.executemany("insert into meta values (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    log.info("stats: %s", {k: meta[k] for k in ("eligible_segments", "hours_sample", "single_job_sample",
                                                 "combo_overlap_factor")})
    return meta


def _symptom_jobs(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        create or replace table symptom_jobs as
        with comp as (select list(job_code) as codes from job_catalog where category = 'component'),
        s as (
          select e.service_order_id, e.symptom, e.make, e.base_model, e.division,
                 nullif(array_to_string(list_filter(e.jobs, lambda j: list_contains(comp.codes, j)), '+'), '') as fix
          from eligible e, comp where e.symptom is not null
          union all
          -- segments with a symptom but no job at all are 'unresolved' (diagnosed, no part-identified fix)
          select g.service_order_id, g.symptom, g.make, g.base_model, g.division, null
          from scoped g where g.symptom is not null and g.n_jobs = 0
        ),
        lv as (
          select 'global' as level, '' as key, * from s
          union all select 'division', division, * from s
          union all select 'base', make || '|' || base_model, * from s
        )
        select level, key, symptom, coalesce(fix, 'UNRESOLVED') as job_key, count(*) as n,
               count(*) / sum(count(*)) over (partition by level, key, symptom) as share,
               sum(count(*)) over (partition by level, key, symptom) as n_symptom
        from lv group by level, key, symptom, coalesce(fix, 'UNRESOLVED')
    """)
