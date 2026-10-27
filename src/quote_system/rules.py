"""Loads the editable rule files in ``config/`` and offers the same text matching in Python
that the pipeline does in SQL, so the API interprets 'replace battery' exactly like the pipeline
interpreted historical segment descriptions."""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PM_INTERVALS = (250, 500, 750, 1000, 1500, 2000, 3000, 4000, 4500, 5000, 6000)
PM_INTERVAL_RE = r"(\d{3,4}) ?-?(H|HR|HRS|HOUR|HOURS|HEURES?)\b"
# jobs that describe *how* a fix was found rather than a fix; dropped when a real repair is present
MODIFIER_JOBS = ("DIAGNOSE", "INSPECTION")


def normalize_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.upper()).strip()


@dataclass(frozen=True)
class Job:
    job_code: str
    display_name: str
    category: str  # component | service
    text_pattern: str
    text_exclude: str
    evidence_pattern: str
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class ComponentRule:
    pattern: str
    role: str
    components: tuple[str, ...]
    verify: bool
    note: str


@dataclass(frozen=True)
class Symptom:
    symptom_code: str
    display_name: str
    pattern: str


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


@lru_cache(maxsize=8)
def load_jobs(config_dir: Path) -> list[Job]:
    jobs = []
    for r in _rows(config_dir / "jobs.csv"):
        jobs.append(Job(r["job_code"].strip(), r["display_name"].strip(), r["category"].strip(),
                        (r.get("text_pattern") or "").strip(), (r.get("text_exclude") or "").strip(),
                        (r.get("evidence_pattern") or "").strip(),
                        tuple(s.strip() for s in (r.get("synonyms") or "").split(";") if s.strip())))
    return jobs


def pm_job_codes() -> list[tuple[str, str]]:
    return [(f"PM_{h}", f"{h} hour service") for h in PM_INTERVALS]


def job_display_names(config_dir: Path) -> dict[str, str]:
    names = {j.job_code: j.display_name for j in load_jobs(config_dir)}
    names.update(dict(pm_job_codes()))
    return names


@lru_cache(maxsize=8)
def load_component_rules(config_dir: Path) -> list[ComponentRule]:
    out = []
    for r in _rows(config_dir / "component_rules.csv"):
        comps = tuple(c.strip() for c in (r.get("components") or "").split("|") if c.strip())
        out.append(ComponentRule(r["pattern"].strip(), r["role"].strip(), comps,
                                 (r.get("verify") or "0").strip() in {"1", "true", "yes"}, r.get("note") or ""))
    return out


@lru_cache(maxsize=8)
def load_overrides(config_dir: Path) -> list[dict]:
    p = config_dir / "part_overrides.csv"
    return _rows(p) if p.exists() else []


@lru_cache(maxsize=8)
def load_symptoms(config_dir: Path) -> list[Symptom]:
    return [Symptom(r["symptom_code"].strip(), r["display_name"].strip(), r["pattern"].strip())
            for r in _rows(config_dir / "symptoms.csv")]


def jobs_from_text(text: str, config_dir: Path) -> list[str]:
    """Python twin of the SQL labelling used in the pipeline (text side only)."""
    t = normalize_text(text)
    found: list[str] = []
    for j in load_jobs(config_dir):
        if j.text_pattern and re.search(j.text_pattern, t) and not (j.text_exclude and re.search(j.text_exclude, t)):
            code = j.job_code
            if code == "PM_SERVICE":
                m = re.search(PM_INTERVAL_RE, t)
                if m and int(m.group(1)) in PM_INTERVALS:
                    code = f"PM_{int(m.group(1))}"
            found.append(code)
    if any(_is_component(c, config_dir) or c.startswith("PM_") or c == "PDI" for c in found):
        found = [c for c in found if c not in MODIFIER_JOBS]
    return sorted(set(found))


def _is_component(code: str, config_dir: Path) -> bool:
    return any(j.job_code == code and j.category == "component" for j in load_jobs(config_dir))


def symptoms_from_text(text: str, config_dir: Path) -> list[str]:
    t = normalize_text(text)
    return [s.symptom_code for s in load_symptoms(config_dir) if re.search(s.pattern, t)]
