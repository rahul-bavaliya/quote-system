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

from .config import Settings
from .rules import jobs_from_text, normalize_text, symptoms_from_text

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
