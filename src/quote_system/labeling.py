"""Step 2 - part taxonomy and segment labelling.

* Every part number is classified as primary (identifies the repair), consumable, core, fee or other
  using ``config/component_rules.csv``. Ambiguous truncated descriptions ('ENGINE C' can be an
  engine cranking motor or an engine control unit) are resolved by checking the text of the work
  order segments the part was used on.
* Every segment gets a *job set*: components from its primary parts, plus components / service
  jobs found in its description. Symptoms ('NO START') are recorded separately - they are the
  question, the parts are the answer.
"""
from __future__ import annotations

import logging

import duckdb
import pandas as pd

from .config import Settings
from .rules import (MODIFIER_JOBS, PM_INTERVAL_RE, PM_INTERVALS, load_component_rules, load_jobs,
                    load_overrides, load_symptoms)

log = logging.getLogger(__name__)

ACTION_RE = (r"R ?& ?R|\bRR\b|REPLAC|\bRPLC|\bREPL\b|INSTALL|CHANGE|\bNEW\b|REMPLAC|SUPPLY|REMOVE|SWAP|"
             r"REBUILD|RESEAL|RE-SEAL|OVERHAUL|REPAIR|\bFIX|REPAR|PROGRAM|FLASH|UPDATE|MISE A JOUR|RECHARGE|REGAS")


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _text_sql(col: str = "description") -> str:
    return f"regexp_replace(upper(strip_accents(coalesce({col}, ''))), '\\s+', ' ', 'g')"


def classify_parts(con: duckdb.DuckDBPyConnection, settings: Settings) -> pd.DataFrame:
    cfg = settings.config_dir
    rules = load_component_rules(cfg)
    jobs = {j.job_code: j for j in load_jobs(cfg)}

    con.execute("""
        create or replace table part_desc as
        select part_number, first(part_description order by n desc, part_description) as description, sum(n) as lines
        from (select part_number, part_description, count(*) n from parts group by all)
        group by part_number
    """)
    case = " ".join(f"when regexp_matches(description, {_q(r.pattern)}) then {i}" for i, r in enumerate(rules))
    con.execute(f"create or replace table part_rule as select *, case {case} else null end as rule_idx from part_desc")
    pr = con.execute("select part_number, description, lines, rule_idx from part_rule").df()

    rule_df = pd.DataFrame([{"rule_idx": i, "role": r.role, "candidates": list(r.components), "verify": r.verify,
                             "rule": r.pattern} for i, r in enumerate(rules)])
    pr = pr.merge(rule_df, on="rule_idx", how="left")
    pr["role"] = pr["role"].fillna("other")
    pr["candidates"] = pr["candidates"].apply(lambda v: v if isinstance(v, list) else [])
    pr["verify"] = pr["verify"].fillna(False).astype(bool)

    # --- text evidence per part: share of its segments whose description supports each component ---
    comp_jobs = [j for j in jobs.values() if j.category == "component"]
    ev_cols = ", ".join(
        f"regexp_matches(t, {_q(j.evidence_pattern or j.text_pattern)}) as ev_{j.job_code}, "
        f"(regexp_matches(t, {_q(j.text_pattern)}) and not regexp_matches(t, {_q(j.text_exclude or '^$NEVER')})) as tx_{j.job_code}"
        for j in comp_jobs)
    con.execute(f"create or replace temp table seg_ev as select service_order_id, {ev_cols} "
                f"from (select service_order_id, {_text_sql()} as t from workorders)")
    sums = ", ".join(f"sum(ev_{j.job_code}::int) as ev_{j.job_code}, sum(tx_{j.job_code}::int) as tx_{j.job_code}"
                     for j in comp_jobs)
    ev = con.execute(f"""
        select p.part_number, count(*) as n_segments, {sums}
        from (select distinct part_number, service_order_id from parts) p
        join seg_ev s using (service_order_id)
        group by p.part_number
    """).df()
    pr = pr.merge(ev, on="part_number", how="left")
    pr["n_segments"] = pr["n_segments"].fillna(0).astype(int)

    # core returns: 'CR' + an existing part number (e.g. CRTY25879B is the core for battery TY25879B)
    known = set(pr["part_number"])
    is_core = pr["part_number"].str.startswith("CR") & pr["part_number"].str[2:].isin(known)

    comps, roles, sources, shares = [], [], [], []
    for row, core in zip(pr.itertuples(index=False), is_core):
        role, comp, source, share = row.role, "", "rule" if pd.notna(row.rule_idx) else "none", None
        n = row.n_segments
        if core:
            role, source = "core", "core_prefix"
        elif role == "primary":
            cands = row.candidates
            if row.verify and n >= settings.vote_min_segments:
                scored = sorted(((getattr(row, f"ev_{c}", 0) or 0) / n, c) for c in cands)
                best_share, best = scored[-1]
                if best_share >= settings.verify_min_share:
                    comp, source, share = best, "rule_verified", best_share
                else:
                    role, source, share = "other", "rule_rejected", best_share
            elif row.verify and len(cands) > 1:
                role, source = "other", "rule_ambiguous_low_volume"
            else:
                comp = cands[0] if cands else ""
        elif role == "other" and n >= settings.discover_min_segments:
            scored = sorted(((getattr(row, f"tx_{j.job_code}", 0) or 0) / n, j.job_code) for j in comp_jobs)
            best_share, best = scored[-1]
            if best_share >= settings.discover_min_share:
                role, comp, source, share = "primary", best, "text_vote", best_share
        comps.append(comp); roles.append(role); sources.append(source); shares.append(share)
    pr["component"], pr["role"], pr["source"], pr["evidence_share"] = comps, roles, sources, shares

    for o in load_overrides(cfg):
        m = pr["part_number"] == o["part_number"].strip().upper()
        pr.loc[m, ["role", "component", "source"]] = [o["role"].strip(), (o.get("component") or "").strip(), "override"]

    out = pr[["part_number", "description", "role", "component", "source", "evidence_share", "n_segments", "lines", "rule"]]
    con.register("part_tax_df", out)
    con.execute("create or replace table part_taxonomy as select * from part_tax_df")
    con.unregister("part_tax_df")
    log.info("taxonomy: %s", out.groupby("role").size().to_dict())
    return out


def label_segments(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    cfg = settings.config_dir
    jobs = load_jobs(cfg)
    comp_codes = [j.job_code for j in jobs if j.category == "component"]

    text_jobs = []
    for j in jobs:
        if not j.text_pattern:
            continue
        cond = f"regexp_matches(t, {_q(j.text_pattern)})"
        if j.text_exclude:
            cond += f" and not regexp_matches(t, {_q(j.text_exclude)})"
        code = _q(j.job_code)
        if j.job_code == "PM_SERVICE":
            ivals = ", ".join(str(i) for i in PM_INTERVALS)
            code = (f"case when try_cast(regexp_extract(t, {_q(PM_INTERVAL_RE)}, 1) as int) in ({ivals}) "
                    f"then 'PM_' || cast(try_cast(regexp_extract(t, {_q(PM_INTERVAL_RE)}, 1) as int) as varchar) "
                    f"else 'PM_SERVICE' end")
        text_jobs.append(f"case when {cond} then {code} end")
    sym = ", ".join(f"case when regexp_matches(t, {_q(s.pattern)}) then {_q(s.symptom_code)} end"
                    for s in load_symptoms(cfg))
    comp_list = "[" + ", ".join(_q(c) for c in comp_codes) + "]"
    mods = "[" + ", ".join(_q(m) for m in MODIFIER_JOBS) + "]"

    con.execute(f"""
        create or replace table segments as
        with base as (
          select w.*, {_text_sql('w.description')} as t,
                 case when regexp_matches(regexp_replace(w.model, '[A-Z]+$', ''), '[0-9]') then regexp_replace(w.model, '[A-Z]+$', '')
                      else w.model end as base_model
          from workorders w
        ),
        part_jobs as (
          select p.service_order_id,
                 list_sort(list_distinct(list(t.component) filter (where t.role = 'primary' and t.component <> ''))) as part_jobs,
                 count(*) as n_part_lines
          from parts p join part_taxonomy t using (part_number)
          group by 1
        ),
        txt as (
          select service_order_id,
                 list_filter([{', '.join(text_jobs)}], lambda x: x is not null) as text_jobs,
                 list_filter([{sym}], lambda x: x is not null) as symptoms,
                 regexp_matches(t, {_q(ACTION_RE)}) as has_action
          from base
        ),
        merged as (
          select b.* exclude (t), b.t as norm_text,
                 coalesce(pj.part_jobs, []) as part_jobs, coalesce(pj.n_part_lines, 0) as n_part_lines,
                 x.text_jobs, x.symptoms,
                 -- a component named only in the text counts when the parts confirm it or the text says it
                 -- was replaced/repaired ('REPLACE BATTERY' yes, 'BATTERY DEAD' no - that is a symptom)
                 list_sort(list_distinct(list_concat(
                     coalesce(pj.part_jobs, []),
                     list_filter(x.text_jobs, lambda j: not list_contains({comp_list}, j)
                                 or x.has_action or list_contains(coalesce(pj.part_jobs, []), j))))) as all_jobs
          from base b left join part_jobs pj using (service_order_id) join txt x using (service_order_id)
        )
        select * exclude (all_jobs),
               case when len(list_filter(all_jobs, lambda j: list_contains({comp_list}, j) or j like 'PM\\_%' escape '\\' or j = 'PDI')) > 0
                    then list_filter(all_jobs, lambda j: not list_contains({mods}, j)) else all_jobs end as jobs,
               symptoms[1] as symptom
        from merged
    """)
    con.execute("""
        alter table segments add column job_key varchar;
        alter table segments add column n_jobs integer;
        update segments set job_key = nullif(array_to_string(jobs, '+'), ''), n_jobs = len(jobs);
    """)
    summary = con.execute("""
        select count(*) segs, count(*) filter (where n_jobs = 0) unlabeled,
               count(*) filter (where n_jobs = 1) single_job, count(*) filter (where n_jobs > 1) multi_job,
               count(*) filter (where len(part_jobs) > 0) with_part_jobs
        from segments
    """).df().iloc[0].to_dict()
    log.info("labelling: %s", summary)
