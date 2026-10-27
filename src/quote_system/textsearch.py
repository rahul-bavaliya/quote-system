"""Context search over historical segment descriptions.

Two signals, both learned from your own work-order text (no external model download):
  * TF-IDF on normalised words + word pairs  -> exact-ish wording ("VALVE LASH ADJUSTMENT")
  * LSA (SVD of the TF-IDF matrix)            -> meaning from co-occurrence
                                                ("SET ENGINE VALVES" ~ "VALVE LASH, ADJUST")
Normalisation handles abbreviations (R&R, ADJ, HYD, A/C), light stemming (ADJUSTMENT -> ADJUST,
VALVES -> VALVE) and common French service words (AJUSTEMENT, REMPLACER, FUITE ...).
"""
from __future__ import annotations

import csv
import os
import re
import time
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import scipy.sparse as sp

from .rules import normalize_text

ABBR = {
    "ADJ": "ADJUST", "RR": "REPLACE", "REPL": "REPLACE", "RPLC": "REPLACE", "R/R": "REPLACE",
    "HYD": "HYDRAULIC", "HYDR": "HYDRAULIC", "A/C": "AC", "ENG": "ENGINE", "TRANS": "TRANSMISSION", "CYL": "CYLINDER",
    "HRS": "HOUR", "HR": "HOUR", "VALVESET": "VALVE SET", "PREFORM": "PERFORM", "D/R": "DIAGNOSE REPAIR",
    "DIAG": "DIAGNOSE", "INSP": "INSPECT", "ELEC": "ELECTRICAL", "W/": "WITH", "BRKT": "BRACKET", "ASSY": "ASSEMBLY",
    "LH": "LEFT", "RH": "RIGHT", "FRT": "FRONT", "RR.": "REAR",
    # French
    "REMPLACER": "REPLACE", "REMPLACEMENT": "REPLACE", "CHANGER": "REPLACE", "FUITE": "LEAK", "MOTEUR": "ENGINE",
    "SOUPAPE": "VALVE", "SOUPAPES": "VALVE", "AJUSTER": "ADJUST", "AJUSTEMENT": "ADJUST", "BATTERIE": "BATTERY",
    "BATTERIES": "BATTERY", "DEMARREUR": "STARTER", "POMPE": "PUMP", "BOYAU": "HOSE", "BOYAUX": "HOSE",
    "VERIFIER": "CHECK", "REPARER": "REPAIR", "REPARATION": "REPAIR", "FREIN": "BRAKE", "FREINS": "BRAKE",
    "PNEU": "TIRE", "PNEUS": "TIRE", "LAVAGE": "WASH", "ENTRETIEN": "SERVICE", "HUILE": "OIL", "VITRE": "GLASS",
    "SIEGE": "SEAT", "DEMARRE": "START", "DEPLACEMENT": "TRAVEL",
}
STOP = set("""A AN THE AND OR TO ON FOR OF IN AT AS PER IS ARE BE WITH FROM BY IF PLEASE PERFORM COMPLETE DO
CUSTOMER REQUEST CUST REQ UNIT MACHINE NEEDS NEED SEG SEGMENT WO DE LA LE LES DU DES ET SUR AU UN UNE""".split())


def _stem(w: str) -> str:
    for suf in ("MENTS", "MENT"):
        if w.endswith(suf) and len(w) > len(suf) + 3:
            return _stem(w[: -len(suf)])
    if w.endswith("IES") and len(w) > 4:
        return w[:-3] + "Y"
    if w.endswith("ING") and len(w) > 5:
        w = w[:-3]
        if len(w) > 2 and w[-1] == w[-2]:
            w = w[:-1]
        return w
    if w.endswith("ED") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("S") and not w.endswith("SS") and len(w) > 3:
        w = w[:-1]
    if w.endswith("E") and len(w) > 4:
        w = w[:-1]
    return w


@lru_cache(maxsize=4)
def _synonyms(config_dir: str) -> list[tuple[re.Pattern, str]]:
    path = Path(config_dir) / "synonyms.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [(re.compile(r["pattern"]), f" {r['replacement'].strip()} ") for r in csv.DictReader(f)
                if (r.get("pattern") or "").strip()]


def _config_dir() -> str:
    return os.environ.get("QS_CONFIG_DIR") or str(Path(__file__).resolve().parents[2] / "config")


def tokens(text: str) -> list[str]:
    t = normalize_text(text)
    for pat, rep in _synonyms(_config_dir()):
        t = pat.sub(rep, t)
    t = re.sub(r"(\d+)\s*(HRS?|HOURS?|H)\b", r"\1 HOUR", t)
    out: list[str] = []
    for w in re.findall(r"[A-Z0-9&/']+", t):
        for x in ABBR.get(w, w).split():
            x = x.strip("/'&")
            if not x or x in STOP or (x.isdigit() and len(x) < 3):
                continue
            if "/" in x:
                out += [_stem(y) for y in x.split("/") if y and y not in STOP]
            else:
                out.append(_stem(x))
    return out


def analyze(text: str) -> list[str]:
    """Analyzer for TfidfVectorizer: tokens + adjacent pairs (module level so it can be pickled)."""
    tk = tokens(text)
    # pairs are order-free: 'REPLACE ARM CYLINDER' and 'ARM CYLINDER, R&R' describe the same job
    return tk + [" ".join(sorted((a, b))) for a, b in zip(tk, tk[1:], strict=False)]


W_TFIDF, W_LSA = 0.6, 0.4


def index_dir_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "_textindex")


def build_index(texts: list[str], counts: list[int], out_dir: Path, dims: int = 128) -> dict:
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    vec = TfidfVectorizer(analyzer=analyze, min_df=2, sublinear_tf=True, dtype=np.float32)
    X = vec.fit_transform(texts).astype(np.float32).tocsr()
    k = max(2, min(dims, X.shape[1] - 1, X.shape[0] - 1))
    svd = TruncatedSVD(k, random_state=0, n_iter=4)
    Z = normalize(svd.fit_transform(X)).astype(np.float32)
    joblib.dump({"vec": vec, "svd": svd, "texts": np.array(texts, dtype=object),
                 "counts": np.array(counts, dtype=np.int32)}, out_dir / "model.joblib")
    sp.save_npz(out_dir / "tfidf.npz", X)
    np.save(out_dir / "lsa.npy", Z)
    return {"descriptions": len(texts), "vocab": X.shape[1], "dims": k, "seconds": round(time.time() - t0, 1)}


class TextIndex:
    def __init__(self, out_dir: Path):
        m = joblib.load(out_dir / "model.joblib")
        self.vec, self.svd, self.texts, self.counts = m["vec"], m["svd"], m["texts"], m["counts"]
        self.X = sp.load_npz(out_dir / "tfidf.npz").tocsr()
        self.XT = self.X.T.tocsr()
        self.Z = np.load(out_dir / "lsa.npy", mmap_mode="r")
        self.row_of = {t: i for i, t in enumerate(self.texts)}

    def score_texts(self, query: str, texts: list[str]) -> dict[str, float]:
        """Similarity of ``query`` to specific indexed descriptions (same 0-1 scale as search)."""
        q = self.vec.transform([query])
        idx = [self.row_of[t] for t in texts if t in self.row_of]
        if q.nnz == 0 or not idx:
            return {}
        s_t = np.asarray((self.X[idx] @ q.T).todense()).ravel()
        qz = self.svd.transform(q).ravel()
        norm = np.linalg.norm(qz)
        s_l = np.asarray(self.Z[idx] @ (qz / norm)) if norm > 0 else np.zeros_like(s_t)
        sc = W_TFIDF * s_t + W_LSA * np.clip(s_l, 0, 1)
        return {self.texts[i]: float(v) for i, v in zip(idx, sc, strict=False)}

    def search(self, query: str, limit: int = 300, min_score: float = 0.55, rel: float = 0.75) -> list[tuple[str, float, int]]:
        """Descriptions whose meaning matches the query: (text, score 0-1, segments)."""
        q = self.vec.transform([query])
        if q.nnz == 0:
            return []
        s_t = np.asarray((self.X @ q.T).todense()).ravel()
        qz = self.svd.transform(q)
        norm = np.linalg.norm(qz)
        s_l = (np.asarray(self.Z @ (qz.ravel() / norm))) if norm > 0 else np.zeros_like(s_t)
        score = W_TFIDF * s_t + W_LSA * np.clip(s_l, 0, 1)
        top = np.argpartition(-score, min(limit, len(score) - 1))[:limit]
        top = top[np.argsort(-score[top])]
        best = float(score[top[0]]) if len(top) else 0.0
        cut = max(min_score, rel * best)
        return [(self.texts[i], round(float(score[i]), 3), int(self.counts[i])) for i in top if score[i] >= cut]
