"""Similar machines, learned from history.

There is no machine-type or size field in the exports, so similarity is measured from what was
done to each model:
  * parts profile   - models that use the same part numbers share a platform / size class
                      (50G and 60G share engine, hydraulic and undercarriage parts)
  * work profile    - models with the same kinds of work are the same machine type
                      (STICK, SWING, THUMB, TRACK -> excavator; LIFT ARM, BUCKET LEVEL -> loader)
similarity = 0.6 * cosine(parts) + 0.4 * cosine(work), within the same make.

config/machine_types.csv (optional) pins models to a machine type; when a model has a type, its
neighbours are limited to the same type. Result: table model_neighbors (make, model, neighbor, sim, rank).
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import numpy as np
import scipy.sparse as sp

from .textsearch import tokens

log = logging.getLogger(__name__)
W_PARTS, W_WORK = 0.6, 0.4


def load_machine_types(config_dir: Path) -> list[tuple[str, re.Pattern, str]]:
    p = Path(config_dir) / "machine_types.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [(r["make"].strip().upper(), re.compile(r["model_pattern"].strip()), r["machine_type"].strip())
                for r in csv.DictReader(f) if (r.get("model_pattern") or "").strip()]


def machine_type_of(make: str, model: str, types: list) -> str | None:
    for mk, pat, typ in types:
        if mk in (make, "*") and pat.fullmatch(model):
            return typ
    return None


def build_neighbors(con, settings, min_segments: int = 30, top_k: int = 8, min_sim: float = 0.25) -> dict:
    from sklearn.feature_extraction.text import TfidfTransformer, TfidfVectorizer
    from sklearn.preprocessing import normalize

    models = con.execute(f"""select make, model from models
                             where n_segments >= {int(min_segments)} and model <> 'UNKNOWN' order by make, model""").fetchall()
    con.execute("create or replace table model_neighbors (make varchar, model varchar, neighbor varchar, sim double, "
                "rank integer, machine_type varchar)")
    if len(models) < 2:
        return {"models": len(models), "pairs": 0}
    idx = {m: i for i, m in enumerate(models)}
    types = load_machine_types(settings.config_dir)
    mtype = [machine_type_of(mk, md, types) for mk, md in models]

    rows, cols, vals, pn_id = [], [], [], {}
    for mk, md, pn, n in con.execute("""
            select s.make, s.model, p.part_number, count(distinct p.service_order_id)
            from parts p join segments s using (service_order_id) join part_taxonomy t using (part_number)
            where t.role <> 'fee' group by all""").fetchall():
        i = idx.get((mk, md))
        if i is not None:
            rows.append(i)
            cols.append(pn_id.setdefault(pn, len(pn_id)))
            vals.append(np.log1p(n))
    P = sp.csr_matrix((vals, (rows, cols)), shape=(len(models), max(1, len(pn_id))))
    P = normalize(TfidfTransformer().fit_transform(P))

    docs = [""] * len(models)
    for mk, md, txt in con.execute("""select make, model, string_agg(norm_text, ' | ')
                                      from (select make, model, norm_text from segments
                                            where norm_text <> '' qualify row_number() over
                                            (partition by make, model order by service_order_id) <= 3000)
                                      group by all""").fetchall():
        i = idx.get((mk, md))
        if i is not None:
            docs[i] = txt or ""
    try:
        T = normalize(TfidfVectorizer(analyzer=tokens, min_df=2, max_df=0.5, sublinear_tf=True).fit_transform(docs))
    except ValueError:  # tiny data sets: no vocabulary left after min_df / max_df
        T = sp.csr_matrix((len(models), 1))

    out = []
    by_make: dict[str, list[int]] = {}
    for i, (mk, _) in enumerate(models):
        by_make.setdefault(mk, []).append(i)
    for mk, ids in by_make.items():
        if len(ids) < 2:
            continue
        sub = np.array(ids)
        S = (W_PARTS * (P[sub] @ P[sub].T) + W_WORK * (T[sub] @ T[sub].T)).toarray()
        np.fill_diagonal(S, 0)
        for a, i in enumerate(ids):
            order = np.argsort(-S[a])
            rank = 0
            for b in order:
                if S[a, b] < min_sim or rank >= top_k:
                    break
                j = ids[b]
                if mtype[i] and mtype[j] != mtype[i]:
                    continue  # pinned machine types must match
                rank += 1
                out.append((mk, models[i][1], models[j][1], round(float(S[a, b]), 4), rank, mtype[i]))
    con.executemany("insert into model_neighbors values (?, ?, ?, ?, ?, ?)", out)
    con.execute("create index if not exists ix_nb on model_neighbors (make, model)")
    review = settings.db_path.parent / "review"
    review.mkdir(parents=True, exist_ok=True)
    con.execute(f"""copy (select make, model, machine_type, string_agg(neighbor || ' (' || round(sim, 2) || ')', ', '
                                 order by rank) as similar_machines
                          from model_neighbors group by all order by make, model)
                   to '{review / "similar_machines.csv"}' (header)""")
    stats = {"models": len(models), "pairs": len(out), "typed_models": sum(1 for t in mtype if t)}
    log.info("similar machines: %s", stats)
    return stats
