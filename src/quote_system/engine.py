"""Quote engine shared by the API and the backtest.

Labour statistics are loaded into memory once (a few hundred thousand small rows); kits, evidence
and symptom tables are queried from DuckDB on demand.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np

from .config import Settings
from .rules import jobs_from_text, normalize_text, symptoms_from_text
from .textsearch import TextIndex, index_dir_for

LEVEL_ORDER = ("global", "division", "make_div", "base", "model")  # least -> most specific
PCTS = ("p25", "p50", "p80", "p90")


def base_model_of(model: str) -> str:
    stripped = re.sub(r"[A-Z]+$", "", model)
    return stripped if re.search(r"[0-9]", stripped) else model


@dataclass
class Machine:
    make: str
    model: str
    base_model: str
    division: str | None
    known_model: bool

    def key(self, level: str) -> str | None:
        if level == "global":
            return ""
        if level == "division":
            return self.division
        if level == "make_div":
            return f"{self.make}|{self.division}" if self.division else None
        if level == "base":
            return f"{self.make}|{self.base_model}"
        return f"{self.make}|{self.model}"


def _wquantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    if w.sum() <= 0:
        return float(np.quantile(values, q))
    cw = (np.cumsum(w) - 0.5 * w) / w.sum()
    return float(np.interp(q, cw, v))


class EngineNotReady(RuntimeError):
    pass


class QuoteEngine:
    def __init__(self, settings: Settings, db_path: Path | None = None):
        self.settings = settings
        self.db_path = Path(db_path or settings.db_path)
        if not self.db_path.exists():
            raise EngineNotReady(f"{self.db_path} not found - run `make pipeline` first")
        self._lock = threading.Lock()
        self.con = duckdb.connect(str(self.db_path), read_only=True)
        self._load()
        idx = index_dir_for(self.db_path)
        self.text_index = TextIndex(idx) if (idx / "model.joblib").exists() else None

    # ------------------------------------------------------------------ loading
    def _load(self) -> None:
        c = self.con.cursor()
        self.stats: dict[tuple[str, str, str], dict] = {}
        for row in c.execute("select level, job_key, key, n, p25, p50, p80, p90, mean, eq_n, eq_p50 from labor_stats").fetchall():
            lvl, job, key, n, p25, p50, p80, p90, mean, eq_n, eq_p50 = row
            self.stats[(job, lvl, key)] = {"n": n, "p25": p25, "p50": p50, "p80": p80, "p90": p90,
                                           "mean": mean, "eq_n": eq_n, "eq_p50": eq_p50}
        self.models: dict[tuple[str, str], tuple[str, str, int]] = {
            (mk, md): (base, div, n) for mk, md, base, div, n in
            c.execute("select make, model, base_model, division, n_segments from models").fetchall()}
        self.catalog = {code: {"job_code": code, "display_name": name, "category": cat,
                               "synonyms": [s for s in (syn or "").split(";") if s]}
                        for code, name, cat, syn in
                        c.execute("select job_code, display_name, category, synonyms from job_catalog").fetchall()}
        self.meta = {k: json.loads(v) for k, v in c.execute("select k, v from meta").fetchall()}
        self.overlap = float(self.meta.get("combo_overlap_factor") or 1.0)
        # history volume per job for search ranking
        self.job_volume = {job: s["n"] for (job, lvl, _), s in self.stats.items() if lvl == "global"}

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self.con.cursor()

    # ------------------------------------------------------------------ machines
    def machine(self, make: str, model: str, division: str | None = None) -> Machine:
        mk, md = normalize_text(make), normalize_text(model)
        hit = self.models.get((mk, md))
        if hit:
            base, div, _ = hit
            return Machine(mk, md, base, division or div, True)
        base = base_model_of(md)
        # unknown model: borrow the division from models sharing the base model (e.g. 333G -> 333)
        divs = [(n, d) for (m2, _), (b2, d, n) in self.models.items() if m2 == mk and b2 == base]
        if not divs:
            divs = [(n, d) for (m2, _), (_, d, n) in self.models.items() if m2 == mk]
        div = division or (max(divs)[1] if divs else None)
        return Machine(mk, md, base, div, False)

    # ------------------------------------------------------------------ labour
    def labor_estimate(self, job_key: str, m: Machine) -> dict | None:
        k = self.settings.shrink_k
        est: dict | None = None
        basis = []
        used_level, used_n = None, 0
        for lvl in LEVEL_ORDER:
            key = m.key(lvl)
            if key is None:
                continue
            s = self.stats.get((job_key, lvl, key))
            if not s:
                continue
            basis.append({"level": lvl, "key": key, "n": s["n"], "p50": round(s["p50"], 2), "p80": round(s["p80"], 2)})
            if est is None:
                est = {p: s[p] for p in PCTS}
            else:
                n = s["n"]
                est = {p: (n * s[p] + k * est[p]) / (n + k) for p in PCTS}
            if s["n"] >= self.settings.min_level_n:
                used_level, used_n = lvl, s["n"]
        if est is None:
            return None
        n_model = (self.stats.get((job_key, "model", m.key("model"))) or {}).get("n", 0)
        n_base = (self.stats.get((job_key, "base", m.key("base"))) or {}).get("n", 0)
        if n_model >= 20:
            conf = "high"
        elif n_model >= 5 or n_base >= 20:
            conf = "medium"
        else:
            conf = "low"
        eq = None
        for lvl in reversed(LEVEL_ORDER):
            key = m.key(lvl)
            s = self.stats.get((job_key, lvl, key)) if key is not None else None
            if s and (s["eq_n"] or 0) >= 3:
                eq = {"level": lvl, "n": s["eq_n"], "p50": round(s["eq_p50"], 2)}
                break
        return {
            "job_key": job_key,
            "p25_hours": round(est["p25"], 2), "p50_hours": round(est["p50"], 2),
            "p80_hours": round(est["p80"], 2), "p90_hours": round(est["p90"], 2),
            "level_used": used_level or "global", "sample_size": used_n, "confidence": conf,
            "historical_eq_hours": eq, "basis": list(reversed(basis)),
        }

    def combo_estimate(self, jobs: list[str], m: Machine) -> dict | None:
        key = "+".join(sorted(jobs))
        g = self.stats.get((key, "global", ""))
        if g and g["n"] >= self.settings.combo_min_n:
            e = self.labor_estimate(key, m)
            if e:
                e["method"] = "historical_combo"
                return e
        singles = [self.labor_estimate(j, m) for j in jobs]
        singles = [s for s in singles if s]
        if not singles:
            return None
        out = {"job_key": key, "method": "sum_with_overlap", "overlap_factor": round(self.overlap, 3),
               "level_used": "per-job", "sample_size": min(s["sample_size"] for s in singles),
               "confidence": min((s["confidence"] for s in singles), key=["low", "medium", "high"].index)}
        for p in ("p25", "p50", "p80", "p90"):
            vals = [s[f"{p}_hours"] for s in singles]
            out[f"{p}_hours"] = round(max(max(vals), sum(vals) * self.overlap), 2)
        if g:
            out["combo_history_n"] = g["n"]
        return out

    # ------------------------------------------------------------------ parts
    def parts_kit(self, job: str, m: Machine) -> dict:
        c = self.cursor()
        for lvl in ("model", "base", "make_div"):
            key = m.key(lvl)
            if key is None:
                continue
            row = c.execute("select n_job_segments from kit_levels where level=? and job_code=? and key=?",
                            [lvl, job, key]).fetchone()
            if not row or row[0] < self.settings.kit_min_segments:
                continue
            rows = c.execute("""
                select part_number, description, role, component, attach_rate, median_qty, n_with_part
                from part_kits where level=? and job_code=? and key=? order by attach_rate desc, part_number
            """, [lvl, job, key]).fetchall()
            kit, optional = [], []
            for pn, desc, role, comp, rate, qty, nwp in rows:
                item = {"part_number": pn, "description": desc, "role": role, "component": comp or None,
                        "attach_rate": round(rate, 3), "qty": round(qty, 2) if qty is not None else None,
                        "seen_on_segments": nwp}
                (kit if rate >= self.settings.kit_attach else optional).append(item)
            # the part that *is* the job (the starter for a starter R&R) goes in the kit even when the
            # history is split between several part numbers (reman vs new, superseded numbers)
            if job in self.catalog and self.catalog[job]["category"] == "component" \
                    and not any(p["component"] == job for p in kit):
                main = [p for p in optional if p["component"] == job and p["role"] == "primary"]
                if main:
                    optional.remove(main[0])
                    main[0]["main_part"] = True
                    main[0]["alternates"] = [p["part_number"] for p in main[1:]]
                    kit.insert(0, main[0])
            return {"job_code": job, "level_used": lvl, "segments": row[0], "kit": kit, "optional": optional[:15]}
        return {"job_code": job, "level_used": None, "segments": 0, "kit": [], "optional": [],
                "note": "not enough single-job history on this make/model to build a parts kit"}

    # ------------------------------------------------------------------ evidence
    def similar(self, job_key: str, m: Machine, limit: int = 5) -> list[dict]:
        c = self.cursor()
        for col, val in (("model", m.model), ("base_model", m.base_model)):
            rows = c.execute(f"""
                select service_order_id, wo_number, wo_segment, branch, invoice_date, description, actual_hours, eq_hours, model
                from segments where job_key = ? and make = ? and {col} = ? and status = 'Invoiced' and actual_hours > 0
                order by invoice_date desc limit ?""", [job_key, m.make, val, limit]).fetchall()
            if rows:
                return [{"service_order_id": r[0], "wo_number": r[1], "segment": r[2], "branch": r[3],
                         "invoice_date": str(r[4]), "description": r[5], "actual_hours": r[6],
                         "eq_hours": r[7], "model": r[8]} for r in rows]
        return []

    # ------------------------------------------------------------------ resolution / search
    def resolve_jobs(self, text: str) -> list[str]:
        codes = [c for c in jobs_from_text(text, self.settings.config_dir) if c in self.catalog]
        if codes:
            return codes
        t = normalize_text(text)
        for code, j in self.catalog.items():  # exact synonym / code
            if t in {normalize_text(s) for s in j["synonyms"]} or t in (code, normalize_text(j["display_name"])):
                return [code]
        return []

    def search_jobs(self, q: str = "", limit: int = 10) -> list[dict]:
        t = normalize_text(q)
        scored = []
        for code, j in self.catalog.items():
            names = [normalize_text(j["display_name"]), code.replace("_", " ")] + [normalize_text(s) for s in j["synonyms"]]
            if not t:
                score = 0.0
            elif any(t in n for n in names):
                score = 2.0 + (1 if any(n.startswith(t) for n in names) else 0)
            else:
                score = max(difflib.SequenceMatcher(None, t, n).ratio() for n in names)
            if t and score < 0.72:
                continue
            scored.append((score, self.job_volume.get(code, 0), j))
        if t:
            for c in self.resolve_jobs(q):  # regex resolution (handles 'r&r batteries', 'remplacer batterie')
                if c in self.catalog and all(x[2]["job_code"] != c for x in scored):
                    scored.append((2.5, self.job_volume.get(c, 0), self.catalog[c]))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [{**j, "history_segments": vol} for _, vol, j in scored[:limit]]

    def search_models(self, make: str | None, q: str = "", limit: int = 20) -> list[dict]:
        mk = normalize_text(make) if make else None
        t = normalize_text(q)
        hits = [(n, m2, md, base, div) for (m2, md), (base, div, n) in self.models.items()
                if (mk is None or m2 == mk) and (not t or md.startswith(t) or t in md)]
        hits.sort(key=lambda x: (not x[2].startswith(t), -x[0]))
        return [{"make": m2, "model": md, "base_model": base, "division": div, "segments": n}
                for n, m2, md, base, div in hits[:limit]]

    def makes(self) -> list[dict]:
        agg: dict[str, int] = {}
        for (mk, _), (_, _, n) in self.models.items():
            agg[mk] = agg.get(mk, 0) + n
        return [{"make": k, "segments": v} for k, v in sorted(agg.items(), key=lambda x: -x[1])]

    # ------------------------------------------------------------------ diagnose
    def diagnose(self, symptom_text: str, m: Machine | None, limit: int = 8) -> dict:
        codes = symptoms_from_text(symptom_text, self.settings.config_dir)
        sym = codes[0] if codes else normalize_text(symptom_text).replace(" ", "_")
        c = self.cursor()
        tiers = []
        if m:
            tiers += [("base", m.key("base")), ("division", m.division)]
        tiers.append(("global", ""))
        for lvl, key in tiers:
            if key is None:
                continue
            rows = c.execute("""select job_key, n, share, n_symptom from symptom_jobs
                                where level=? and key=? and symptom=? order by n desc""", [lvl, key, sym]).fetchall()
            if rows and rows[0][3] >= 20:
                out = []
                for job_key, n, share, _ in rows[:limit]:
                    jobs_ = [] if job_key == "UNRESOLVED" else job_key.split("+")
                    item = {"job_key": job_key, "segments": n, "share": round(share, 3), "jobs": jobs_,
                            "display_name": " + ".join(self.catalog.get(j, {}).get("display_name", j) for j in jobs_)
                            or "No part-identified fix (diagnosis / adjustment only)"}
                    if m and job_key != "UNRESOLVED":
                        est = self.labor_estimate(job_key, m) or {}
                        item["p50_hours"] = est.get("p50_hours")
                        item["p80_hours"] = est.get("p80_hours")
                    out.append(item)
                return {"symptom": sym, "matched_symptoms": codes, "level_used": lvl, "key": key,
                        "segments_with_symptom": rows[0][3], "likely_fixes": out}
        return {"symptom": sym, "matched_symptoms": codes, "level_used": None, "segments_with_symptom": 0,
                "likely_fixes": []}

    # ------------------------------------------------------------------ quote
    def quote(self, make: str, model: str, jobs: list[str], include_parts: bool = True,
              division: str | None = None) -> dict:
        m = self.machine(make, model, division)
        labor, missing = [], []
        for j in jobs:
            e = self.labor_estimate(j, m)
            if e:
                e["display_name"] = self.catalog.get(j, {}).get("display_name", j)
                labor.append(e)
            else:
                missing.append(j)
        total = None
        if len(jobs) == 1 and labor:
            total = {k: labor[0][k] for k in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")}
            total["method"] = "single_job"
        elif len(jobs) > 1:
            ce = self.combo_estimate(jobs, m)
            if ce:
                total = {k: ce[k] for k in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")}
                total["method"] = ce["method"]
                if ce["method"] == "historical_combo":
                    total["sample_size"], total["level_used"] = ce["sample_size"], ce["level_used"]
                else:
                    total["overlap_factor"] = ce["overlap_factor"]
        parts = [self.parts_kit(j, m) for j in jobs] if include_parts else []
        key = "+".join(sorted(jobs))
        evidence = self.similar(key, m) or (self.similar(jobs[0], m) if jobs else [])
        warnings = []
        if not m.known_model:
            warnings.append(f"Model {m.model} not in history; using base model {m.base_model} / division {m.division}.")
        if missing:
            warnings.append(f"No history for: {', '.join(missing)}")
        if any(l["confidence"] == "low" for l in labor):
            warnings.append("Low confidence: little history for this job on this machine - review before sending.")
        return {
            "machine": {"make": m.make, "model": m.model, "base_model": m.base_model, "division": m.division,
                        "known_model": m.known_model},
            "jobs": jobs, "labor": labor, "total": total, "parts": parts,
            "parts_included": include_parts, "similar_work_orders": evidence, "warnings": warnings,
        }

    # ------------------------------------------------------------------ context (similar-history) quotes
    POOL_COLS = """service_order_id, wo_number, wo_segment, branch, invoice_date, description, norm_text,
                   make, model, base_model, division, actual_hours, eq_hours"""

    def similar_quote(self, text: str, m: Machine, include_parts: bool = True) -> dict | None:
        """Quote from past segments whose description *means* the same thing as ``text``.

        Works for any job, including ones with no job code ('VALVE LASH ADJUSTMENT')."""
        if self.text_index is None:
            return None
        matches = self.text_index.search(text)
        if not matches:
            return None
        # precision first: use the tightest similarity cut that still leaves enough history
        best = matches[0][1]
        for frac in (0.95, 0.9, 0.85, 0.8, 0.75):
            chosen = [x for x in matches if x[1] >= frac * best]
            if sum(n for _, _, n in chosen) >= self.settings.similar_min_pool:
                break
        matches = chosen
        score_of = {t: sc for t, sc, _ in matches}
        rows = self.cursor().execute(f"select {self.POOL_COLS} from text_pool where norm_text in (select unnest(?))",
                                     [list(score_of)]).fetchall()
        if not rows:
            return None
        weights = {r[0]: score_of.get(r[6], 0.0) for r in rows}
        out = self._pool_quote(rows, weights, m, include_parts, text.strip(), best)
        if out is None:
            return None
        model_counts: dict[str, int] = {}
        for r in out.pop("_model_rows"):
            model_counts[r[6]] = model_counts.get(r[6], 0) + 1
        out["matched_descriptions"] = sorted(
            ({"description": t, "score": sc, "segments": n, "on_this_model": model_counts.get(t, 0)} for t, sc, n in matches),
            key=lambda d: (-d["score"], -d["on_this_model"], -d["segments"]))[:15]
        if best < 0.7:
            out["warnings"].insert(0, "Closest past descriptions are only a loose match - check the matched descriptions.")
        out.update({"mode": "similar_history", "query": text})
        out["total"]["method"] = "similar_history"
        return out

    # ------------------------------------------------------------------ parts-based quotes
    def resolve_parts(self, part_numbers: list[str]) -> tuple[list[dict], list[str]]:
        """Map user part numbers to known ones (case/space/dash-insensitive). Returns (known, unknown)."""
        c = self.cursor()
        known, unknown = [], []
        for raw in part_numbers:
            pn = re.sub(r"\s+", "", str(raw).upper())
            if not pn:
                continue
            key = re.sub(r"[^A-Z0-9]", "", pn)
            row = c.execute("""select i.part_number, i.idf, i.segs, t.description, t.role, t.component
                               from part_idf i left join part_taxonomy t using (part_number)
                               where i.part_number = ? or i.pn_key = ? order by (i.part_number = ?) desc, i.segs desc limit 1""",
                            [pn, key, pn]).fetchone()
            if row:
                known.append({"input": raw, "part_number": row[0], "idf": row[1], "history_segments": row[2],
                              "description": row[3], "role": row[4], "component": row[5] or None})
            else:
                unknown.append(raw)
        return known, unknown

    def parts_quote(self, part_numbers: list[str], m: Machine, include_parts: bool = True,
                    text: str | None = None) -> dict | None:
        """Labour from past segments that used the same parts (idf-weighted cosine: a starter
        identifies a job, an o-ring barely does). Optional ``text`` re-ranks by description.

        Parts for two or more different repairs (battery + starter) are quoted per repair and
        combined like combined jobs, so one rare part cannot hide the other repair."""
        known, unknown = self.resolve_parts(part_numbers)
        if not known:
            return None
        seen = {}
        for k_ in known:
            seen.setdefault(k_["part_number"], k_)
        known = list(seen.values())
        comp_of = {k_["part_number"]: k_["component"] for k_ in known if k_["role"] == "primary" and k_["component"]}
        for k_ in known:  # core returns belong to the repair of their part (CRSE502823 -> SE502823)
            if k_["role"] == "core" and k_["part_number"].startswith("CR"):
                base = self.resolve_parts([k_["part_number"][2:]])[0]
                if base and base[0]["component"]:
                    comp_of[k_["part_number"]] = base[0]["component"]
        comps = sorted(set(comp_of.values()))
        if len(comps) < 2:
            return self._parts_pool(known, unknown, m, include_parts, text)
        others = [k_ for k_ in known if k_["part_number"] not in comp_of]
        subs = []
        for c in comps:
            grp = [k_ for k_ in known if comp_of.get(k_["part_number"]) == c] + others
            sub = self._parts_pool(grp, [], m, include_parts, text)
            if sub:
                subs.append((c, sub))
        if not subs:
            return None
        if len(subs) == 1:
            return subs[0][1]
        singles = [sub["total"] for _, sub in subs]
        combo_key = "+".join(c for c, _ in subs)
        g = self.stats.get((combo_key, "global", ""))
        factor, how = self.overlap, "overlap factor"
        if g and g["n"] >= self.settings.combo_min_n:
            single_g = [self.stats.get((c, "global", "")) for c, _ in subs]
            if all(single_g):
                factor, how = g["p50"] / sum(x["p50"] for x in single_g), f"{g['n']} past {combo_key} jobs"
        total = {p: round(max(max(t[p] for t in singles), sum(t[p] for t in singles) * factor), 2)
                 for p in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")}
        total.update({"method": "parts_history_combined", "overlap_factor": round(factor, 3), "overlap_basis": how})
        first = subs[0][1]
        warnings = [f"Parts cover {len(subs)} repairs ({', '.join(c for c, _ in subs)}): quoted separately and combined."]
        if unknown:
            warnings.append(f"Not found in parts history (ignored): {', '.join(map(str, unknown))}")
        return {
            "mode": "parts_history", "query": text or "", "machine": first["machine"], "jobs": [],
            "labor": [l for _, sub in subs for l in sub["labor"]], "total": total,
            "parts": [p for _, sub in subs for p in sub["parts"]], "parts_included": include_parts,
            "matched_segments": sum(sub["matched_segments"] for _, sub in subs),
            "matched_descriptions": [d for _, sub in subs for d in sub["matched_descriptions"][:8]],
            "similar_work_orders": [w for _, sub in subs for w in sub["similar_work_orders"][:4]],
            "parts_query": known, "unknown_parts": unknown,
            "inferred_jobs": [{"job_code": c, "display_name": self.catalog.get(c, {}).get("display_name", c)} for c, _ in subs],
            "warnings": warnings + [w for _, sub in subs for w in sub["warnings"]],
        }

    def _parts_pool(self, known: list[dict], unknown: list, m: Machine, include_parts: bool,
                    text: str | None) -> dict | None:
        qnorm = float(np.sqrt(sum(k_["idf"] ** 2 for k_ in known)))
        rows = self.cursor().execute(f"""
            with q as (select unnest(?) as part_number, unnest(?) as idf),
            hit as (select service_order_id, sum(q.idf * q.idf) as dot
                    from (select distinct service_order_id, part_number from parts
                          where part_number in (select part_number from q)) p
                    join q using (part_number) group by 1)
            select {", ".join("t." + c.strip() for c in self.POOL_COLS.split(","))}, hit.dot / (? * n.norm) as score
            from hit join seg_part_norm n using (service_order_id) join text_pool t using (service_order_id)
            order by score desc limit 50000
        """, [[k_["part_number"] for k_ in known], [k_["idf"] for k_ in known], qnorm]).fetchall()
        if not rows:
            return None
        scores = {r[0]: float(r[13]) for r in rows}
        if text and self.text_index is not None:
            ts = self.text_index.score_texts(text, list({r[6] for r in rows}))
            scores = {r[0]: scores[r[0]] * (0.5 + 0.5 * ts.get(r[6], 0.0)) for r in rows}
        ranked = sorted(rows, key=lambda r: -scores[r[0]])
        best = scores[ranked[0][0]]
        for frac in (0.9, 0.8, 0.7, 0.6, 0.5):
            chosen = [r for r in ranked if scores[r[0]] >= max(frac * best, 0.2)]
            if len(chosen) >= self.settings.similar_min_pool:
                break
        rows = [r[:13] for r in chosen]
        if not rows:
            return None
        title = ", ".join(f"{k_['part_number']}" + (f" ({k_['description']})" if k_["description"] else "") for k_ in known)
        main = sorted({k_["component"] for k_ in known if k_["role"] == "primary" and k_["component"]})
        title = (" + ".join(self.catalog.get(c, {}).get("display_name", c) for c in main) + f" (parts {title})"
                 if main else f"Parts {title}")
        out = self._pool_quote(rows, scores, m, include_parts, title, best)
        if out is None:
            return None
        model_rows = out.pop("_model_rows")
        # what were these parts used for? most common descriptions in the matched history
        desc_n: dict[str, int] = {}
        for r in rows:
            desc_n[r[6]] = desc_n.get(r[6], 0) + 1
        on_model: dict[str, int] = {}
        for r in model_rows:
            on_model[r[6]] = on_model.get(r[6], 0) + 1
        out["matched_descriptions"] = [
            {"description": d, "score": round(n / len(rows), 3), "segments": n, "on_this_model": on_model.get(d, 0)}
            for d, n in sorted(desc_n.items(), key=lambda x: (-on_model.get(x[0], 0), -x[1]))[:15]]
        jobs = sorted({k_["component"] for k_ in known if k_["role"] == "primary" and k_["component"]})
        out.update({"mode": "parts_history", "query": text or "", "parts_query": known, "unknown_parts": unknown,
                    "inferred_jobs": [{"job_code": j, "display_name": self.catalog.get(j, {}).get("display_name", j)}
                                      for j in jobs]})
        out["total"]["method"] = "parts_history"
        if unknown:
            out["warnings"].insert(0, f"Not found in parts history (ignored): {', '.join(map(str, unknown))}")
        if all(k_["role"] in ("consumable", "fee", "hardware", "core") for k_ in known):
            out["warnings"].insert(0, "Only consumables/hardware given - they are used on many different jobs, "
                                      "so add the main part or a job description for a tighter quote.")
        return out

    # ------------------------------------------------------------------ shared: quote from a pool of past segments
    def _pool_quote(self, rows: list, weights: dict, m: Machine, include_parts: bool, title: str,
                    best: float) -> dict | None:
        hours = np.array([r[11] for r in rows], dtype=float)
        lh = np.log(hours)
        med = np.median(lh)
        mad = max(1.4826 * np.median(np.abs(lh - med)), 0.1)
        keep = np.abs(lh - med) / mad <= self.settings.outlier_mad_z
        rows = [r for r, k_ in zip(rows, keep) if k_]
        keys = {
            "global": lambda r: "", "division": lambda r: r[10], "make_div": lambda r: f"{r[7]}|{r[10]}",
            "base": lambda r: f"{r[7]}|{r[9]}", "model": lambda r: f"{r[7]}|{r[8]}",
        }
        k = self.settings.shrink_k
        est, basis, used_level, used_n, level_rows = None, [], None, 0, {}
        for lvl in LEVEL_ORDER:
            key = m.key(lvl)
            if key is None:
                continue
            sub = [r for r in rows if keys[lvl](r) == key]
            if not sub:
                continue
            level_rows[lvl] = sub
            h = np.array([r[11] for r in sub])
            w = np.array([weights.get(r[0], 0.0) for r in sub]) ** 4  # closer matches count more
            st = {p: _wquantile(h, w, q) for p, q in (("p25", .25), ("p50", .5), ("p80", .8), ("p90", .9))}
            n = len(sub)
            basis.append({"level": lvl, "key": key, "n": n, "p50": round(float(st["p50"]), 2),
                          "p80": round(float(st["p80"]), 2)})
            est = st if est is None else {p: (n * st[p] + k * est[p]) / (n + k) for p in PCTS}
            if n >= self.settings.min_level_n:
                used_level, used_n = lvl, n
        if est is None:
            return None
        n_model = len(level_rows.get("model", []))
        n_base = len(level_rows.get("base", []))
        conf = "high" if n_model >= 20 else "medium" if (n_model >= 5 or n_base >= 20) else "low"
        if best < 0.7 and conf != "low":
            conf = "medium" if conf == "high" else "low"
        eqs = [r[12] for r in rows if r[12] and r[12] > 0]
        labor = {
            "job_key": "SIMILAR", "display_name": title,
            **{f"{p}_hours": round(float(est[p]), 2) for p in PCTS},
            "level_used": used_level or "global", "sample_size": used_n, "confidence": conf,
            "historical_eq_hours": {"level": "global", "n": len(eqs), "p50": round(float(np.median(eqs)), 2)} if len(eqs) >= 3 else None,
            "basis": list(reversed(basis)),
        }
        # the most specific level with enough segments drives evidence and parts
        spec = next((lvl for lvl in ("model", "base", "make_div", "division", "global")
                     if len(level_rows.get(lvl, [])) >= self.settings.kit_min_segments), None) \
            or next(iter(reversed(list(level_rows))))
        pool = level_rows[spec]
        recent = sorted(pool, key=lambda r: (round(weights.get(r[0], 0), 3), r[4]), reverse=True)[:8]
        evidence = [{"service_order_id": r[0], "wo_number": r[1], "segment": r[2], "branch": r[3],
                     "invoice_date": str(r[4]), "description": r[5], "actual_hours": r[11], "eq_hours": r[12],
                     "model": r[8], "match_score": round(weights.get(r[0], 0), 3)} for r in recent]
        parts = [self._kit_from_segments([r[0] for r in pool], spec)] if include_parts else []
        warnings = []
        if not m.known_model:
            warnings.append(f"Model {m.model} not in history; using base model {m.base_model} / division {m.division}.")
        return {
            "machine": {"make": m.make, "model": m.model, "base_model": m.base_model, "division": m.division,
                        "known_model": m.known_model},
            "jobs": [], "labor": [labor],
            "total": {k2: labor[k2] for k2 in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")},
            "parts": parts, "parts_included": include_parts, "matched_segments": len(rows),
            "similar_work_orders": evidence, "warnings": warnings, "_model_rows": level_rows.get("model", []),
        }

    def _kit_from_segments(self, ids: list[str], level: str) -> dict:
        c = self.cursor()
        n = len(ids)
        rows = c.execute("""
            with p as (select service_order_id, part_number, sum(qty) q from parts
                       where service_order_id in (select unnest(?)) and coalesce(qty, 0) > 0 group by all)
            select p.part_number, t.description, t.role, t.component, count(*) segs, median(p.q) qty
            from p left join part_taxonomy t using (part_number)
            group by all order by segs desc, part_number
        """, [ids]).fetchall()
        kit, optional = [], []
        for pn, desc, role, comp, segs, qty in rows:
            rate = segs / n
            if rate < self.settings.kit_optional_attach:
                break
            item = {"part_number": pn, "description": desc, "role": role, "component": comp or None,
                    "attach_rate": round(rate, 3), "qty": round(qty, 2) if qty is not None else None,
                    "seen_on_segments": segs}
            (kit if rate >= self.settings.kit_attach else optional).append(item)
        return {"job_code": "SIMILAR", "level_used": level, "segments": n, "kit": kit, "optional": optional[:15]}

