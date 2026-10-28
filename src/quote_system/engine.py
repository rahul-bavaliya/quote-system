"""Quote engine shared by the API and the backtest.

Labour statistics are loaded into memory once (a few hundred thousand small rows); kits, evidence
and symptom tables are queried from DuckDB on demand.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np

from .config import Settings
from .normalize import MakeResolver, base_of, model_key, series_of
from .rules import jobs_from_text, normalize_text, symptoms_from_text
from .textsearch import TextIndex, index_dir_for

LEVEL_ORDER = ("global", "division", "make_div", "similar", "base", "series", "model")  # least -> most specific
LOCATIONS = {"SHOP": "Shop", "OFFSITE": "Off site"}
PCTS = ("p25", "p50", "p80", "p90")


def normalize_location(loc: str | None) -> str | None:
    if not loc:
        return None
    t = normalize_text(loc).replace(" ", "").replace("-", "")
    if t.startswith("SHOP"):
        return "SHOP"
    if t.startswith(("OFF", "FIELD", "SITE", "ONSITE")):
        return "OFFSITE"
    raise ValueError(f"location must be 'shop' or 'offsite', got {loc!r}")


@dataclass
class Machine:
    make: str
    model: str            # canonical model key, e.g. 135GLC
    series: str           # 135G
    base_model: str       # 135
    division: str | None
    known_model: bool
    display_model: str = ""
    make_input: str = ""
    model_input: str = ""
    make_resolution: str = "exact"
    neighbors: list = field(default_factory=list)   # [(model, similarity)] learned similar machines

    def key(self, level: str) -> str | None:
        if level == "global":
            return ""
        if level == "division":
            return self.division
        if level == "make_div":
            return f"{self.make}|{self.division}" if self.division else None
        if level == "base":
            return f"{self.make}|{self.base_model}"
        if level == "series":
            return f"{self.make}|{self.series}"
        if level == "similar":
            return f"{self.make}|~similar" if self.neighbors else None
        return f"{self.make}|{self.model}"

    def as_dict(self) -> dict:
        d = {"make": self.make, "model": self.display_model or self.model, "model_key": self.model,
             "series": self.series, "base_model": self.base_model, "division": self.division,
             "known_model": self.known_model,
             "similar_machines": [{"model": md, "similarity": round(s, 2)} for md, s in self.neighbors]}
        if self.make_resolution != "exact" or (self.make_input and normalize_text(self.make_input) != self.make):
            d["make_entered"] = self.make_input
            d["make_resolved_by"] = self.make_resolution
        if self.model_input and normalize_text(self.model_input) != (self.display_model or self.model):
            d["model_entered"] = self.model_input
        return d

    def input_warnings(self) -> list[str]:
        """Warnings about how the make was read, shown at the top of every quote."""
        how, entered = self.make_resolution, (self.make_input or "").strip()
        if how == "from_model":
            what = f"'{entered}' is not a known make" if entered else "No make given"
            return [f"{what}; read as {self.make} because only {self.make} has model {self.model} in history - check."]
        if how in ("word", "fuzzy"):
            return [f"Make '{entered}' read as {self.make} (closest known spelling) - check."]
        if how in ("unknown", "empty"):
            what = f"Make '{entered}' is not in history" if entered else "No make given"
            return [f"{what}; this quote uses division / all-make history only, so it is broad. "
                    "Pick a make from /makes, or add the spelling to config/make_aliases.csv."]
        return []


def _wquantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    if w.sum() <= 0:
        return float(np.quantile(values, q))
    cw = (np.cumsum(w) - 0.5 * w) / w.sum()
    return float(np.interp(q, cw, v))


def _join_descriptions(descs: list[str]) -> str:
    """'REPLACE BATTERY' + 'REPLACE STARTER' -> 'REPLACE BATTERY & STARTER'."""
    descs = [d for d in descs if d]
    if not descs:
        return ""
    verbs = {d.split()[0] for d in descs}
    if len(verbs) == 1 and all(len(d.split()) > 1 for d in descs):
        v = verbs.pop()
        return v + " " + " & ".join(d.split(" ", 1)[1] for d in descs)
    return " & ".join(descs)


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
        self.stats_loc: dict[tuple[str, str, str, str], dict] = {}
        for row in c.execute("select level, job_key, key, location, n, p25, p50, p80, p90, mean, eq_n, eq_p50 "
                             "from labor_stats").fetchall():
            lvl, job, key, loc, n, p25, p50, p80, p90, mean, eq_n, eq_p50 = row
            st = {"n": n, "p25": p25, "p50": p50, "p80": p80, "p90": p90, "mean": mean, "eq_n": eq_n, "eq_p50": eq_p50}
            if loc == "ALL":
                self.stats[(job, lvl, key)] = st
            else:
                self.stats_loc[(job, lvl, key, loc)] = st
        self.models: dict[tuple[str, str], dict] = {
            (mk, md): {"series": ser, "base": base, "division": div, "n": n, "display": disp or md}
            for mk, md, ser, base, div, n, disp in
            c.execute("select make, model, series, base_model, division, n_segments, display_model from models").fetchall()}
        make_totals: dict[str, int] = {}
        self.model_makes: dict[str, set[str]] = {}    # model key -> makes that have it (to infer a missing make)
        self.series_makes: dict[str, set[str]] = {}
        for (mk, md), v in self.models.items():
            make_totals[mk] = make_totals.get(mk, 0) + v["n"]
            if mk not in ("UNKNOWN", "MISC", "SHOP"):
                self.model_makes.setdefault(md, set()).add(mk)
                self.series_makes.setdefault(v["series"], set()).add(mk)
        make_map = dict(c.execute("select raw_clean, canonical from make_map").fetchall())
        self.neighbors: dict[tuple[str, str], list[tuple[str, float]]] = {}
        try:
            nb_rows = c.execute("select make, model, neighbor, sim from model_neighbors order by make, model, rank").fetchall()
            for mk, md, nb, sim in nb_rows:
                self.neighbors.setdefault((mk, md), []).append((nb, sim))
        except duckdb.CatalogException:  # database built before similar machines existed
            pass
        self._sim_cache: dict = {}
        self.makes_resolver = MakeResolver(make_map, make_totals, str(self.settings.config_dir))
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
        """'John Deere' / '135G LC' -> JD / 135GLC (series 135G, base 135), same rules as the pipeline."""
        mk, how = self.makes_resolver.resolve(make)
        md = model_key(model)
        if how in ("unknown", "empty") and md != "UNKNOWN":
            # make missing or unreadable: if only one make has this model (or series) in history, use it
            ser0 = series_of(md)
            for owners in (self.model_makes.get(md), self.series_makes.get(ser0)):
                if owners and len(owners) == 1:
                    mk, how = next(iter(owners)), "from_model"
                    break
        hit = self.models.get((mk, md))
        if hit:
            return Machine(mk, md, hit["series"], hit["base"], division or hit["division"], True, hit["display"],
                           make, model, how, self._neighbors_for(mk, md, hit["series"], hit["base"]))
        ser = series_of(md)
        base = base_of(ser)
        # unknown model: borrow the division from models of the same series, then base model, then make
        div = division
        if not div:
            for pick in (lambda v: v["series"] == ser, lambda v: v["base"] == base, lambda v: True):
                cands = [(v["n"], v["division"]) for (m2, _), v in self.models.items() if m2 == mk and pick(v)]
                if cands:
                    div = max(cands)[1]
                    break
        return Machine(mk, md, ser, base, div, False, "", make, model, how, self._neighbors_for(mk, md, ser, base))

    def _neighbors_for(self, mk: str, md: str, ser: str, base: str) -> list[tuple[str, float]]:
        """Learned similar machines; for a model with little / no history, borrow them from the busiest
        model of the same series (or base), which itself counts as the closest neighbour."""
        nb = self.neighbors.get((mk, md))
        if nb:
            return nb
        for pick in (lambda v: v["series"] == ser, lambda v: v["base"] == base):
            cands = [(v["n"], m2) for (k2, m2), v in self.models.items() if k2 == mk and m2 != md and pick(v)
                     and (k2, m2) in self.neighbors]
            if cands:
                anchor = max(cands)[1]
                return [(anchor, 1.0)] + [(n, s) for n, s in self.neighbors[(mk, anchor)] if n != md][:7]
        return []

    # ------------------------------------------------------------------ labour
    BROAD_LEVELS = ("make_div", "division", "global")

    def class_factor(self, m: Machine, ref_level: str, related_jobs: list[str]) -> dict | None:
        """How long this machine class (the model + its similar machines) takes compared with the broader
        level a quote had to fall back to, measured on jobs both have done.

        The ratio of the related job (e.g. HYD_CYLINDER for an arm cylinder) counts most; the median over
        all shared jobs is the fallback. Returns None when there is not enough shared history."""
        ref_key = m.key(ref_level)
        if ref_key is None:
            return None
        ck = ("factor", m.make, m.model, ref_level, tuple(related_jobs))
        if ck in self._sim_cache:
            return self._sim_cache[ck]
        group = [m.model] + [md for md, _ in m.neighbors]
        rows = self.cursor().execute("""select job_key, count(*), median(actual_hours) from hours_clean
            where make = ? and model in (select unnest(?)) and n_jobs = 1 group by 1 having count(*) >= 5""",
                                     [m.make, group]).fetchall()
        comps = {c for c, v in self.catalog.items() if v["category"] == "component"}
        pairs = []
        for job, n, p50 in rows:
            ref = self.stats.get((job, ref_level, ref_key))
            if job in comps and ref and ref["n"] >= 20 and p50 > 0 and ref["p50"] > 0:
                pairs.append((job, int(n), float(p50), float(ref["p50"]), float(np.log(p50 / ref["p50"]))))
        out = None
        if len(pairs) >= 3:
            w = np.array([min(p_[1], 50) for p_ in pairs], dtype=float)
            r = np.array([p_[4] for p_ in pairs])
            order = np.argsort(r)
            overall = float(r[order][np.searchsorted(np.cumsum(w[order]) / w.sum(), 0.5)])
            log_f, basis = overall, f"median of {len(pairs)} shared job types"
            rel = [p_ for p_ in pairs if p_[0] in related_jobs]
            if rel:
                j = max(rel, key=lambda p_: p_[1])
                k = self.settings.shrink_k
                log_f = (j[1] * j[4] + k * overall) / (j[1] + k)
                basis = (f"{self.catalog.get(j[0], {}).get('display_name', j[0])}: {j[2]:.1f} h on similar machines "
                         f"vs {j[3]:.1f} h at {ref_level} level ({j[1]} jobs), shrunk toward the {len(pairs)}-job median")
            f = float(np.clip(np.exp(log_f), self.settings.class_factor_min, self.settings.class_factor_max))
            out = {"factor": round(f, 3), "from_level": ref_level, "basis": basis, "shared_jobs": len(pairs),
                   "machine_group": group}
        self._sim_cache[ck] = out
        return out

    def _apply_factor(self, est: dict, used_level: str, m: Machine, related: list[str]) -> dict | None:
        """Scale an estimate that came from a broad level to this machine class. Mutates est."""
        if used_level not in self.BROAD_LEVELS or not m.neighbors:
            return None
        cf = self.class_factor(m, used_level, related)
        if not cf or abs(np.log(cf["factor"])) < 0.05:
            return None
        for p_ in PCTS:
            est[p_] = est[p_] * cf["factor"]
        return cf

    def _similar_stat(self, job_key: str, m: Machine, location: str | None) -> dict | None:
        """Hours of this job on the learned similar machines, weighted by similarity."""
        ck = (job_key, m.make, m.model, location)
        if ck in self._sim_cache:
            return self._sim_cache[ck]
        sims = dict(m.neighbors)
        rows = self.cursor().execute(f"""
            select model, actual_hours, eq_hours from hours_clean
            where job_key = ? and make = ? and model in (select unnest(?)) {"and location = ?" if location else ""}
        """, [job_key, m.make, list(sims)] + ([location] if location else [])).fetchall()
        out = None
        if rows:
            h = np.array([r[1] for r in rows], dtype=float)
            w = np.array([sims[r[0]] for r in rows], dtype=float)
            eq = [r[2] for r in rows if r[2] and r[2] > 0]
            per: dict[str, int] = {}
            for r in rows:
                per[r[0]] = per.get(r[0], 0) + 1
            out = {"n": len(rows), **{p: _wquantile(h, w, q) for p, q in (("p25", .25), ("p50", .5), ("p80", .8), ("p90", .9))},
                   "mean": float(np.average(h, weights=w)), "eq_n": len(eq), "eq_p50": float(np.median(eq)) if eq else None,
                   "models": [{"model": md, "similarity": round(sims[md], 2), "jobs": per[md]}
                              for md, _ in m.neighbors if md in per]}
        if len(self._sim_cache) > 20000:
            self._sim_cache.clear()
        self._sim_cache[ck] = out
        return out

    def _stat(self, job_key: str, lvl: str, key: str, location: str | None, m: Machine | None = None) -> dict | None:
        if lvl == "similar":
            return self._similar_stat(job_key, m, location) if m is not None else None
        if location:
            return self.stats_loc.get((job_key, lvl, key, location))
        return self.stats.get((job_key, lvl, key))

    def labor_estimate(self, job_key: str, m: Machine, location: str | None = None) -> dict | None:
        k = self.settings.shrink_k
        similar_used: list = []
        scaled, scale_checked, broad_level = None, False, "global"
        est: dict | None = None
        basis = []
        used_level, used_n = None, 0
        for lvl in LEVEL_ORDER:
            key = m.key(lvl)
            if key is None:
                continue
            s = self._stat(job_key, lvl, key, location, m)
            if not s:
                continue
            if est is not None and scaled is None and not scale_checked and lvl not in self.BROAD_LEVELS:
                scale_checked = True  # broad levels describe average machines: bring them to this class first
                scaled = self._apply_factor(est, broad_level, m, job_key.split("+"))
            if lvl in self.BROAD_LEVELS:
                broad_level = lvl
            if lvl == "similar":
                similar_used = s["models"]
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
        if scaled is None and not scale_checked:
            scaled = self._apply_factor(est, broad_level, m, job_key.split("+"))
        n_model = (self._stat(job_key, "model", m.key("model"), location) or {}).get("n", 0)
        n_base = max((self._stat(job_key, "series", m.key("series"), location) or {}).get("n", 0),
                     (self._stat(job_key, "base", m.key("base"), location) or {}).get("n", 0))
        if n_model >= 20:
            conf = "high"
        elif n_model >= 5 or n_base >= 20:
            conf = "medium"
        else:
            conf = "low"
        eq = None
        for lvl in reversed(LEVEL_ORDER):
            key = m.key(lvl)
            s = self._stat(job_key, lvl, key, location, m) if key is not None else None
            if s and (s["eq_n"] or 0) >= 3:
                eq = {"level": lvl, "n": s["eq_n"], "p50": round(s["eq_p50"], 2)}
                break
        return {
            "job_key": job_key,
            "p25_hours": round(est["p25"], 2), "p50_hours": round(est["p50"], 2),
            "p80_hours": round(est["p80"], 2), "p90_hours": round(est["p90"], 2),
            "level_used": used_level or "global", "sample_size": used_n, "confidence": conf,
            "historical_eq_hours": eq, "basis": list(reversed(basis)), "similar_machines_used": similar_used,
            "scaled_to_machine_class": scaled,
        }

    def combo_estimate(self, jobs: list[str], m: Machine, location: str | None = None) -> dict | None:
        key = "+".join(sorted(jobs))
        g = self._stat(key, "global", "", location)
        if g and g["n"] >= self.settings.combo_min_n:
            e = self.labor_estimate(key, m, location)
            if e:
                e["method"] = "historical_combo"
                return e
        singles = [self.labor_estimate(j, m, location) for j in jobs]
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
        for lvl in ("model", "series", "base", "similar", "make_div"):
            key = m.key(lvl)
            if key is None:
                continue
            if lvl == "similar":
                ids = [r[0] for r in c.execute("""select service_order_id from segments
                        where job_key = ? and n_jobs = 1 and status = 'Invoiced' and make = ? and model in (select unnest(?))""",
                                               [job, m.make, [md for md, _ in m.neighbors]]).fetchall()]
                if len(ids) >= self.settings.kit_min_segments:
                    kit = self._kit_from_segments(ids, "similar")
                    kit["job_code"] = job
                    return kit
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
    def similar(self, job_key: str, m: Machine, limit: int = 8) -> list[dict]:
        """Recent past work orders for this job on the searched model only (any spelling of it)."""
        rows = self.cursor().execute("""
            select service_order_id, wo_number, wo_segment, branch, invoice_date, description, actual_hours, eq_hours,
                   coalesce(model_raw, model), location
            from segments where job_key = ? and make = ? and model = ? and status = 'Invoiced' and actual_hours > 0
            order by invoice_date desc limit ?""", [job_key, m.make, m.model, limit]).fetchall()
        return [{"service_order_id": r[0], "wo_number": r[1], "segment": r[2], "branch": r[3],
                 "invoice_date": str(r[4]), "description": r[5], "actual_hours": r[6],
                 "eq_hours": r[7], "model": r[8], "location": r[9]} for r in rows]

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
        mk = self.makes_resolver.resolve(make)[0] if make else None
        t = model_key(q) if q else ""
        t = "" if t == "UNKNOWN" else t
        hits = [(v["n"], m2, md, v) for (m2, md), v in self.models.items()
                if (mk is None or m2 == mk) and (not t or md.startswith(t) or t in md)]
        hits.sort(key=lambda x: (not x[2].startswith(t), -x[0]))
        return [{"make": m2, "model": v["display"], "model_key": md, "series": v["series"], "base_model": v["base"],
                 "division": v["division"], "segments": n} for n, m2, md, v in hits[:limit]]

    def makes(self) -> list[dict]:
        agg: dict[str, int] = {}
        for (mk, _), v in self.models.items():
            agg[mk] = agg.get(mk, 0) + v["n"]
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
              division: str | None = None, location: str | None = None) -> dict:
        m = self.machine(make, model, division)
        labor, missing = [], []
        for j in jobs:
            e = self.labor_estimate(j, m, location) or (self.labor_estimate(j, m) if location else None)
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
            ce = self.combo_estimate(jobs, m, location) or (self.combo_estimate(jobs, m) if location else None)
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
        warnings = m.input_warnings()
        if not m.known_model:
            warnings.append(f"Model {m.model} not in history; using series {m.series} / base model {m.base_model} / division {m.division}.")
        if missing:
            warnings.append(f"No history for: {', '.join(missing)}")
        if any(lab["confidence"] == "low" for lab in labor):
            warnings.append("Low confidence: little history for this job on this machine - review before sending.")
        by_location = {}
        for loc, label in LOCATIONS.items():
            if len(jobs) == 1:
                e = self.labor_estimate(jobs[0], m, loc)
            else:
                e = self.combo_estimate(jobs, m, loc) if jobs else None
            if e:
                by_location[loc] = {"label": label, **{k: e[k] for k in ("p25_hours", "p50_hours", "p80_hours",
                                                                         "p90_hours", "level_used", "sample_size",
                                                                         "confidence")}}
        if total is not None:
            total["location"] = location or "ALL"
        return {
            "machine": m.as_dict(),
            "jobs": jobs, "labor": labor, "total": total, "by_location": by_location, "parts": parts,
            "parts_included": include_parts, "similar_work_orders": evidence, "warnings": warnings,
        }

    # ------------------------------------------------------------------ context (similar-history) quotes
    # row layout used by context / parts quotes (indexes below)
    POOL_COLS = """service_order_id, wo_number, wo_segment, branch, invoice_date, description, norm_text,
                   make, model, base_model, division, actual_hours, eq_hours, series, location, model_raw"""
    N_POOL = 16

    def similar_quote(self, text: str, m: Machine, include_parts: bool = True,
                      location: str | None = None) -> dict | None:
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
        out = self._pool_quote(rows, weights, m, include_parts, text.strip(), best, location,
                               related=self.resolve_jobs(text))
        if out is None:
            return None
        model_rows = out.pop("_model_rows")
        out["suggested_descriptions"] = self.suggest_descriptions(rows, model_rows)
        model_counts: dict[str, int] = {}
        for r in model_rows:
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
                    text: str | None = None, location: str | None = None) -> dict | None:
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
            return self._parts_pool(known, unknown, m, include_parts, text, location)
        others = [k_ for k_ in known if k_["part_number"] not in comp_of]
        subs = []
        for c in comps:
            grp = [k_ for k_ in known if comp_of.get(k_["part_number"]) == c] + others
            sub = self._parts_pool(grp, [], m, include_parts, text, location)
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
        total.update({"method": "parts_history_combined", "overlap_factor": round(factor, 3), "overlap_basis": how,
                      "location": subs[0][1]["total"].get("location", "ALL")})
        by_location = {}
        for loc, label in LOCATIONS.items():
            parts_ = [sub["by_location"].get(loc) for _, sub in subs]
            if all(parts_):
                by_location[loc] = {"label": label, **{p: round(max(max(x[p] for x in parts_), sum(x[p] for x in parts_) * factor), 2)
                                                       for p in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")},
                                    "level_used": "per-repair", "sample_size": min(x["sample_size"] for x in parts_),
                                    "confidence": min((x["confidence"] for x in parts_), key=["low", "medium", "high"].index)}
        first = subs[0][1]
        warnings = [f"Parts cover {len(subs)} repairs ({', '.join(c for c, _ in subs)}): quoted separately and combined."]
        if unknown:
            warnings.append(f"Not found in parts history (ignored): {', '.join(map(str, unknown))}")
        return {
            "mode": "parts_history", "query": text or "", "machine": first["machine"], "jobs": [],
            "labor": [lab for _, sub in subs for lab in sub["labor"]], "total": total, "by_location": by_location,
            "parts": [p for _, sub in subs for p in sub["parts"]], "parts_included": include_parts,
            "matched_segments": sum(sub["matched_segments"] for _, sub in subs),
            "matched_descriptions": [d for _, sub in subs for d in sub["matched_descriptions"][:8]],
            "suggested_descriptions": ([{"description": _join_descriptions([sub["suggested_descriptions"][0]["description"]
                                                                            for _, sub in subs if sub["suggested_descriptions"]]),
                                         "segments": None, "on_this_model": None, "wordings": None, "combined": True,
                                         "same_job_also_written": []}]
                                       + [d for _, sub in subs for d in sub["suggested_descriptions"][:2]]),
            "similar_work_orders": sorted((w for _, sub in subs for w in sub["similar_work_orders"]),
                                          key=lambda w: w["invoice_date"], reverse=True)[:8],
            "parts_query": known, "unknown_parts": unknown,
            "inferred_jobs": [{"job_code": c, "display_name": self.catalog.get(c, {}).get("display_name", c)} for c, _ in subs],
            "warnings": list(dict.fromkeys(warnings + [w for _, sub in subs for w in sub["warnings"]])),
        }

    def _parts_pool(self, known: list[dict], unknown: list, m: Machine, include_parts: bool,
                    text: str | None, location: str | None = None) -> dict | None:
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
        scores = {r[0]: float(r[self.N_POOL]) for r in rows}
        if text and self.text_index is not None:
            ts = self.text_index.score_texts(text, list({r[6] for r in rows}))
            scores = {r[0]: scores[r[0]] * (0.5 + 0.5 * ts.get(r[6], 0.0)) for r in rows}
        ranked = sorted(rows, key=lambda r: -scores[r[0]])
        best = scores[ranked[0][0]]
        for frac in (0.9, 0.8, 0.7, 0.6, 0.5):
            chosen = [r for r in ranked if scores[r[0]] >= max(frac * best, 0.2)]
            if len(chosen) >= self.settings.similar_min_pool:
                break
        rows = [r[:self.N_POOL] for r in chosen]
        if not rows:
            return None
        title = ", ".join(f"{k_['part_number']}" + (f" ({k_['description']})" if k_["description"] else "") for k_ in known)
        main = sorted({k_["component"] for k_ in known if k_["role"] == "primary" and k_["component"]})
        title = (" + ".join(self.catalog.get(c, {}).get("display_name", c) for c in main) + f" (parts {title})"
                 if main else f"Parts {title}")
        out = self._pool_quote(rows, scores, m, include_parts, title, best, location, related=main)
        if out is None:
            return None
        model_rows = out.pop("_model_rows")
        out["suggested_descriptions"] = self.suggest_descriptions(rows, model_rows)
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

    # ------------------------------------------------------------------ suggested quote description
    _NOISE_RE = re.compile(r"WARRANTY|INVOICE|\bSEE\b|\bSEG\b|SEGMENT|\bPO\b|QUOTE|REPORT|^\W*$")
    _ACTION_RE = re.compile(r"REPLAC|R ?& ?R|INSTALL|REBUILD|RESEAL|ADJUST|\bSET\b|OVERHAUL|CHANGE|REMPLAC|AJUST|SERVICE|PROGRAM")
    _SYMPTOM_ONLY_RE = re.compile(r"^(D/R |DIAG\w*|CHECK|INSPECT\w*|INVESTIGAT\w*|TROUBLESHOOT\w*|REPAIR FOR|VERIFIER)")

    def suggest_descriptions(self, rows: list, model_rows: list, limit: int = 4) -> list[dict]:
        """Group past descriptions that mean the same thing and suggest the clearest common wording,
        preferring ones that name the repair ('REPLACE BATTERY') over symptoms ('D/R NO START')."""
        from .textsearch import tokens

        groups: dict[tuple, dict] = {}
        on_model = {r[0] for r in model_rows}
        for r in rows:
            raw = re.sub(r"\s+", " ", (r[5] or "").strip().upper()).strip(" .-")
            if not raw:
                continue
            key = tuple(sorted(set(tokens(raw))))
            if not key:
                continue
            g = groups.setdefault(key, {"n": 0, "model_n": 0, "variants": {}})
            g["n"] += 1
            g["model_n"] += r[0] in on_model
            g["variants"][raw] = g["variants"].get(raw, 0) + 1
        scored = []
        for g in groups.values():
            text = max(g["variants"].items(), key=lambda x: (x[1], -len(x[0])))[0]
            score = g["n"] + 2 * g["model_n"]
            if not self._ACTION_RE.search(text):
                score *= 0.1 if symptoms_from_text(text, self.settings.config_dir) else 0.4
            if self._SYMPTOM_ONLY_RE.search(text):
                score *= 0.5
            if self._NOISE_RE.search(text) or len(text) > 60:
                score *= 0.2
            scored.append((score, text, g))
        scored.sort(key=lambda x: -x[0])
        clean = lambda v: not self._NOISE_RE.search(v) and not re.search(r"[A-Z0-9]*\d[A-Z0-9]*[A-Z][A-Z0-9]*\d{4,}", v) \
            and len(v) <= 60
        out = []
        for _, t, g in scored:
            if g["n"] < 2 and out:
                continue  # one-off wordings are not worth suggesting
            alts = [v for v, c in sorted(g["variants"].items(), key=lambda x: -x[1]) if v != t and c >= 2 and clean(v)][:3]
            out.append({"description": t, "segments": g["n"], "on_this_model": g["model_n"],
                        "wordings": len(g["variants"]), "same_job_also_written": alts})
            if len(out) >= limit:
                break
        if out and out[0]["segments"] < 2:
            return []
        return out

    # ------------------------------------------------------------------ shared: quote from a pool of past segments
    _POOL_KEYS = {
        "global": lambda r: "", "division": lambda r: r[10], "make_div": lambda r: f"{r[7]}|{r[10]}",
        "base": lambda r: f"{r[7]}|{r[9]}", "series": lambda r: f"{r[7]}|{r[13]}", "model": lambda r: f"{r[7]}|{r[8]}",
    }

    def _blend(self, rows: list, weights: dict, m: Machine, related: list[str] | None = None) -> dict | None:
        """Closeness-weighted percentiles per level, blended model <- series <- base <- similar <- ... <- global.
        Broad levels (make+division and up) are first scaled to this machine class (see class_factor)."""
        k = self.settings.shrink_k
        est, basis, used_level, used_n, level_rows = None, [], None, 0, {}
        scaled, scale_checked, broad_level = None, False, "global"
        for lvl in LEVEL_ORDER:
            key = m.key(lvl)
            if key is None:
                continue
            if lvl == "similar":
                sims = dict(m.neighbors)
                sub = [r for r in rows if r[7] == m.make and r[8] in sims]
            else:
                sub = [r for r in rows if self._POOL_KEYS[lvl](r) == key]
            if not sub:
                continue
            if est is not None and scaled is None and not scale_checked and lvl not in self.BROAD_LEVELS:
                scale_checked = True
                scaled = self._apply_factor(est, broad_level, m, related or [])
            if lvl in self.BROAD_LEVELS:
                broad_level = lvl
            level_rows[lvl] = sub
            h = np.array([r[11] for r in sub])
            w = np.array([weights.get(r[0], 0.0) for r in sub]) ** 4  # closer matches count more
            if lvl == "similar":
                w = w * np.array([sims[r[8]] for r in sub])
            st = {p: _wquantile(h, w, q) for p, q in (("p25", .25), ("p50", .5), ("p80", .8), ("p90", .9))}
            n = len(sub)
            basis.append({"level": lvl, "key": key, "n": n, "p50": round(float(st["p50"]), 2),
                          "p80": round(float(st["p80"]), 2)})
            est = st if est is None else {p: (n * st[p] + k * est[p]) / (n + k) for p in PCTS}
            if n >= self.settings.min_level_n:
                used_level, used_n = lvl, n
        if est is None:
            return None
        if scaled is None and not scale_checked:
            scaled = self._apply_factor(est, broad_level, m, related or [])
        n_model = len(level_rows.get("model", []))
        n_mid = max(len(level_rows.get("series", [])), len(level_rows.get("base", [])), len(level_rows.get("similar", [])))
        conf = "high" if n_model >= 20 else "medium" if (n_model >= 5 or n_mid >= 20) else "low"
        sim_used = []
        if "similar" in level_rows:
            per: dict[str, int] = {}
            for r in level_rows["similar"]:
                per[r[8]] = per.get(r[8], 0) + 1
            sim_used = [{"model": md, "similarity": round(s_, 2), "jobs": per[md]} for md, s_ in m.neighbors if md in per]
        return {"est": est, "basis": basis, "level_used": used_level or "global", "sample_size": used_n,
                "confidence": conf, "level_rows": level_rows, "similar_used": sim_used, "scaled": scaled}

    def _pool_quote(self, rows: list, weights: dict, m: Machine, include_parts: bool, title: str,
                    best: float, location: str | None = None, related: list[str] | None = None) -> dict | None:
        hours = np.array([r[11] for r in rows], dtype=float)
        lh = np.log(hours)
        med = np.median(lh)
        mad = max(1.4826 * np.median(np.abs(lh - med)), 0.1)
        keep = np.abs(lh - med) / mad <= self.settings.outlier_mad_z
        rows = [r for r, k_ in zip(rows, keep, strict=False) if k_]
        related = related or []
        allb = self._blend(rows, weights, m, related)
        if allb is None:
            return None
        by_location = {}
        for loc, label in LOCATIONS.items():
            b = self._blend([r for r in rows if r[14] == loc], weights, m, related)
            if b:
                by_location[loc] = {"label": label, **{f"{p}_hours": round(float(b["est"][p]), 2) for p in PCTS},
                                    "level_used": b["level_used"], "sample_size": b["sample_size"],
                                    "confidence": b["confidence"]}
        main = allb
        if location:
            lb = self._blend([r for r in rows if r[14] == location], weights, m, related)
            if lb:
                main = lb
        conf = main["confidence"]
        if best < 0.7 and conf != "low":
            conf = "medium" if conf == "high" else "low"
        eqs = [r[12] for r in rows if r[12] and r[12] > 0]
        labor = {
            "job_key": "SIMILAR", "display_name": title,
            **{f"{p}_hours": round(float(main["est"][p]), 2) for p in PCTS},
            "level_used": main["level_used"], "sample_size": main["sample_size"], "confidence": conf,
            "historical_eq_hours": {"level": "global", "n": len(eqs), "p50": round(float(np.median(eqs)), 2)} if len(eqs) >= 3 else None,
            "basis": list(reversed(main["basis"])), "similar_machines_used": main["similar_used"],
            "scaled_to_machine_class": main.get("scaled"),
        }
        level_rows = allb["level_rows"]
        # the most specific level with enough segments drives evidence and parts
        spec = next((lvl for lvl in ("model", "series", "base", "similar", "make_div", "division", "global")
                     if len(level_rows.get(lvl, [])) >= self.settings.kit_min_segments), None) \
            or next(iter(reversed(list(level_rows))))
        pool = level_rows[spec]
        # evidence: the searched model only (hours above may still borrow from similar machines)
        recent = sorted(level_rows.get("model", []), key=lambda r: (round(weights.get(r[0], 0), 3), r[4]), reverse=True)[:8]
        evidence = [{"service_order_id": r[0], "wo_number": r[1], "segment": r[2], "branch": r[3],
                     "invoice_date": str(r[4]), "description": r[5], "actual_hours": r[11], "eq_hours": r[12],
                     "model": r[15] or r[8], "location": LOCATIONS.get(r[14]),
                     "match_score": round(weights.get(r[0], 0), 3)} for r in recent]
        parts = [self._kit_from_segments([r[0] for r in pool], spec)] if include_parts else []
        warnings = m.input_warnings()
        if not m.known_model:
            warnings.append(f"Model {m.model} not in history; using series {m.series} / base model {m.base_model} / division {m.division}.")
        if location and main is allb:
            warnings.append(f"No {LOCATIONS[location].lower()} history for this match - showing all locations.")
        return {
            "machine": m.as_dict(),
            "jobs": [], "labor": [labor],
            "total": {k2: labor[k2] for k2 in ("p25_hours", "p50_hours", "p80_hours", "p90_hours")}
                     | {"location": location if (location and main is not allb) else "ALL"},
            "by_location": by_location,
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

