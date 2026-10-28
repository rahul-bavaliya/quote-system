"""Make / model standardisation.

The ERP data has the same brand under many spellings (JD, JDE, DEERE, JOHN, 'JD #2', truncated
5-letter codes like HITAC) and the same model written several ways (470G LC, 470GLC, 470G-LC).

Layers, applied in order:
  1. deterministic clean-up   upper-case, accents, punctuation, '#2' unit numbers
                              model key: only letters + digits  ('470G LC' -> '470GLC')
  2. config/make_aliases.csv  your explicit mappings (JOHN DEERE -> JD)
  3. automatic merges         a rare make spelling is folded into a big make only when its models
                              are that make's models (evidence), see `build_make_map`
  4. review report            data/review/make_mapping.csv lists every decision with its evidence;
                              copy lines into make_aliases.csv to fix or force a mapping
Model hierarchy:  model 135GLC -> series 135G (configuration suffix LC/NLC/LR/SC/II/III removed)
                  -> base 135 (generation letters removed) -> make + division -> division -> all.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .rules import normalize_text

# configuration suffixes that do not change the job much (long carriage, narrow, long reach, series II)
SERIES_SUFFIX = re.compile(r"(?:LCR|NLC|LC|LR|SC|ZTS|II|III|IV)+$")


def clean_make(raw: str | None) -> str:
    t = normalize_text(raw)
    t = re.sub(r"#\s*\d*$", "", t)              # JD #2, JD#3
    t = re.sub(r"[^A-Z0-9& ]+", " ", t)          # JD. JD' JD/TP
    t = re.sub(r"\s+\d+$", "", t)                # 'JD  1'
    t = re.sub(r"\s+", " ", t).strip()
    return t or "UNKNOWN"


def model_key(raw: str | None) -> str:
    t = re.sub(r"[^A-Z0-9]", "", normalize_text(raw))
    return t or "UNKNOWN"


def series_of(key: str) -> str:
    s = SERIES_SUFFIX.sub("", key)
    return s if len(s) >= 2 and re.search(r"[0-9]", s) else key


def base_of(series: str) -> str:
    s = re.sub(r"[A-Z]+$", "", series)
    return s if re.search(r"[0-9]", s) else series


@lru_cache(maxsize=4)
def load_make_aliases(config_dir: str) -> dict[str, str]:
    p = Path(config_dir) / "make_aliases.csv"
    if not p.exists():
        return {}
    with open(p, newline="", encoding="utf-8") as f:
        return {clean_make(r["alias"]): clean_make(r["canonical"]) for r in csv.DictReader(f)
                if (r.get("alias") or "").strip() and (r.get("canonical") or "").strip()}


@lru_cache(maxsize=4)
def load_model_aliases(config_dir: str) -> dict[tuple[str, str], str]:
    p = Path(config_dir) / "model_aliases.csv"
    if not p.exists():
        return {}
    with open(p, newline="", encoding="utf-8") as f:
        return {(clean_make(r["make"]), model_key(r["alias"])): model_key(r["canonical"]) for r in csv.DictReader(f)
                if (r.get("alias") or "").strip() and (r.get("canonical") or "").strip()}


@dataclass
class MakeDecision:
    raw_clean: str
    canonical: str
    method: str            # same | alias | auto_prefix | auto_overlap | none
    segments: int
    model_overlap: float | None = None
    candidate: str | None = None


def build_make_map(make_model_counts: list[tuple[str, str, int]], config_dir: str,
                   big_make_min: int = 300, max_ratio: float = 0.05) -> dict[str, MakeDecision]:
    """Decide the canonical make for every cleaned make spelling.

    make_model_counts: (clean_make, model_key, segments)
    A spelling V is folded into a big make C (>= big_make_min segments) when V is much rarer
    (<= max_ratio of C) and V's segments are on models C really has (model overlap):
        * overlap >= 0.5 and V/C are prefix-related or within 2 edits (JDE~JD, HITAC~HIT)
        * overlap >= 0.8 and V has <= 500 segments (DEERE, JOHN -> JD)
    """
    aliases = load_make_aliases(config_dir)
    by_make: dict[str, dict[str, int]] = {}
    for mk, md, n in make_model_counts:
        mk = aliases.get(mk, mk)
        by_make.setdefault(mk, {})
        by_make[mk][md] = by_make[mk].get(md, 0) + n
    totals = {mk: sum(v.values()) for mk, v in by_make.items()}
    big = sorted((mk for mk, n in totals.items() if n >= big_make_min and mk not in ("UNKNOWN", "MISC", "SHOP")),
                 key=lambda m: -totals[m])
    decisions: dict[str, MakeDecision] = {}
    raw_totals: dict[str, int] = {}
    for mk, _, n in make_model_counts:
        raw_totals[mk] = raw_totals.get(mk, 0) + n
    for raw, n in raw_totals.items():
        if raw in aliases and aliases[raw] != raw:
            decisions[raw] = MakeDecision(raw, aliases[raw], "alias", n)
    resolved = {d.raw_clean: d.canonical for d in decisions.values()}

    for v, models in by_make.items():
        if v in big or v in ("UNKNOWN",):
            continue
        n_v = totals[v]
        best = None
        for c in big:
            if n_v > max_ratio * totals[c]:
                continue
            cm = by_make[c]
            shared = sum(cnt for md, cnt in models.items() if cm.get(md, 0) >= 3 and md != "UNKNOWN")
            overlap = shared / n_v
            short = min(len(v), len(c))
            related = (short >= 2 and (c.startswith(v) or v.startswith(c)) and len(v) >= 2) \
                or (short >= 4 and _edit_distance(v, c) <= 2)
            ok = (overlap >= 0.5 and related) or (overlap >= 0.8 and n_v <= 500)
            if ok and (best is None or overlap > best[1]):
                best = (c, overlap, related)
        for raw in [r for r in raw_totals if aliases.get(r, r) == v]:
            if best:
                decisions[raw] = MakeDecision(raw, best[0], "auto_prefix" if best[2] else "auto_overlap",
                                              raw_totals[raw], round(best[1], 3), best[0])
            elif raw not in decisions:
                decisions[raw] = MakeDecision(raw, resolved.get(raw, v), "same" if raw == v else "alias", raw_totals[raw])
    for raw, n in raw_totals.items():
        decisions.setdefault(raw, MakeDecision(raw, aliases.get(raw, raw), "same" if aliases.get(raw, raw) == raw else "alias", n))
    return decisions


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class MakeResolver:
    """Request-time make resolution: 'John Deere', 'jd', 'Deere' -> 'JD'.

    Order (first hit wins): exact -> alias file -> spelling seen in history -> prefix (HITACHI -> HIT)
    -> word (a known make/alias word inside the entry: 'DEERE & CO' -> JD)
    -> fuzzy (closest known spelling within 1-2 letters: 'JHON DEERE', 'JDEERE' -> JD).
    'word' and 'fuzzy' are guesses, so a quote shows a warning for them; a tie between two different
    makes is never guessed and stays 'unknown'."""

    GUESSED = ("word", "fuzzy", "from_model")

    def __init__(self, make_map: dict[str, str], known: dict[str, int], config_dir: str):
        self.map = make_map            # cleaned spelling seen in history -> canonical
        self.known = known             # canonical -> segments
        self.aliases = load_make_aliases(config_dir)
        # every spelling we know -> canonical, for word and fuzzy matching
        self.spellings: dict[str, str] = {}
        for k in known:
            self.spellings[k] = k
        for s, canon in self.map.items():
            self.spellings.setdefault(s, canon)
        for s, a in self.aliases.items():
            self.spellings.setdefault(s, self.map.get(a, a))
        self.spellings.pop("UNKNOWN", None)

    def resolve(self, raw: str | None) -> tuple[str, str]:
        """Returns (canonical make, how it was resolved)."""
        c = clean_make(raw)
        if c == "UNKNOWN":
            return c, "empty"
        if c in self.known:
            return c, "exact"
        if c in self.aliases:
            a = self.aliases[c]
            return self.map.get(a, a), "alias"
        if c in self.map:
            return self.map[c], "history_spelling"
        squashed = c.replace(" ", "")
        for k in sorted(self.known, key=lambda k: -self.known[k]):  # 'HITACHI' -> HIT, 'TOYOTA' -> TOYOT
            if len(k) >= 3 and (squashed.startswith(k) or (len(squashed) >= 3 and k.startswith(squashed))):
                return k, "prefix"
        hit = self._by_word(c)
        if hit:
            return hit, "word"
        hit = self._fuzzy(squashed)
        if hit:
            return hit, "fuzzy"
        return c, "unknown"

    def _by_word(self, c: str) -> str | None:
        """'DEERE & CO', 'JOHN DEER' -> JD: a word of the entry is itself a known make or alias."""
        found = {self.spellings[w] for w in c.split() if len(w) >= 4 and w in self.spellings}
        return found.pop() if len(found) == 1 else None

    def _fuzzy(self, squashed: str) -> str | None:
        """Closest known spelling, at most 1 edit (short words) or 2 edits (8+ letters)."""
        if len(squashed) < 4:
            return None
        limit = 2 if len(squashed) >= 8 else 1
        best_d, found = limit + 1, set()
        for s, canon in self.spellings.items():
            s2 = s.replace(" ", "")
            if len(s2) < 4:
                continue
            d = _edit_distance(squashed, s2)
            if d < best_d:
                best_d, found = d, {canon}
            elif d == best_d:
                found.add(canon)
        return found.pop() if best_d <= limit and len(found) == 1 else None


def standardize(con, settings) -> dict:
    """Pipeline step: rewrite workorders.make/model to canonical values, add series/base_model,
    keep the originals as make_raw/model_raw, and write review files to data/review/."""
    cfg = str(settings.config_dir)
    pairs = con.execute("select make, model, count(*) from workorders group by all").fetchall()
    keyed: dict[tuple[str, str], int] = {}
    rows = []
    for mk, md, n in pairs:
        cm, k = clean_make(mk), model_key(md)
        keyed[(cm, k)] = keyed.get((cm, k), 0) + n
        rows.append((mk, md, cm, k, n))
    decisions = build_make_map([(a, b, n) for (a, b), n in keyed.items()], cfg)
    model_alias = load_model_aliases(cfg)
    out = []
    for mk, md, cm, k, n in rows:
        canon = decisions[cm].canonical
        k = model_alias.get((canon, k), k)
        ser = series_of(k)
        out.append((mk, md, canon, k, ser, base_of(ser), n))
    con.execute("create or replace temp table mm (make_raw varchar, model_raw varchar, make varchar, model varchar, "
                "series varchar, base_model varchar, n bigint)")
    con.executemany("insert into mm values (?, ?, ?, ?, ?, ?, ?)", out)
    con.execute("""
        create or replace table workorders as
        select w.* exclude (make, model), w.make as make_raw, w.model as model_raw,
               mm.make, mm.model, mm.series, mm.base_model
        from workorders w join mm on mm.make_raw is not distinct from w.make and mm.model_raw is not distinct from w.model
    """)
    con.execute("""
        create or replace table model_display as
        select make, model, first(model_raw order by n desc, model_raw) as display_model,
               count(distinct model_raw) as spellings, sum(n) as n
        from mm group by make, model
    """)
    con.execute("create or replace table make_map (raw_clean varchar, canonical varchar, method varchar, "
                "segments bigint, model_overlap double)")
    con.executemany("insert into make_map values (?, ?, ?, ?, ?)",
                    [(d.raw_clean, d.canonical, d.method, d.segments, d.model_overlap) for d in decisions.values()])
    review = settings.db_path.parent / "review"
    review.mkdir(parents=True, exist_ok=True)
    con.execute(f"""copy (select raw_clean as make_as_written, canonical, method, segments, model_overlap
                         from make_map where raw_clean <> canonical order by segments desc)
                   to '{review / "make_mapping.csv"}' (header)""")
    con.execute(f"""copy (select m.make, m.model as model_key, d.display_model, list(distinct m.model_raw) as written_as,
                                 sum(m.n) as segments
                          from mm m join model_display d using (make, model)
                          where d.spellings > 1 group by all order by segments desc)
                   to '{review / "model_variants.csv"}' (header)""")
    raw_makes = len({cm for _, _, cm, _, _ in rows})
    stats = {
        "makes_as_written": raw_makes,
        "makes_canonical": len({d.canonical for d in decisions.values()}),
        "make_spellings_merged": sum(1 for d in decisions.values() if d.canonical != d.raw_clean),
        "segments_with_merged_make": sum(d.segments for d in decisions.values() if d.canonical != d.raw_clean),
        "models_as_written": len({(mk, md) for mk, md, *_ in rows}),
        "models_canonical": con.execute("select count(*) from model_display").fetchone()[0],
    }
    return stats

