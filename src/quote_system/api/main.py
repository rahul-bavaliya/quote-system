"""FastAPI service.

    uvicorn quote_system.api.main:app --reload     (or: make run)

Docs at /docs, the quoting page at /.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..config import get_settings
from ..engine import EngineNotReady, QuoteEngine
from .store import QuoteStore

log = logging.getLogger("quote_system.api")
STATIC = Path(__file__).parent / "static"


class State:
    engine: QuoteEngine | None = None
    error: str | None = None
    store: QuoteStore | None = None


state = State()


def load_engine() -> None:
    s = get_settings()
    try:
        new = QuoteEngine(s)
        old, state.engine, state.error = state.engine, new, None
        if old:
            old.con.close()
    except EngineNotReady as e:
        state.engine, state.error = None, str(e)
        log.warning(str(e))
    if state.store is None:
        state.store = QuoteStore(s.quotes_db_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_engine()
    yield


app = FastAPI(title="Service Quote System", version="0.1.0", lifespan=lifespan,
              description="Labour hours + parts kits for a job on a make/model, learned from historical work orders.")


def engine() -> QuoteEngine:
    if state.engine is None:
        raise HTTPException(503, state.error or "statistics database not built - run `make pipeline`")
    return state.engine


# ---------------------------------------------------------------- schemas
class QuoteRequest(BaseModel):
    make: str = Field(..., examples=["JD"])
    model: str = Field(..., examples=["333G"])
    job_text: str | None = Field(None, description="Free text, e.g. 'replace battery' or 'battery and starter R&R'",
                                 examples=["replace batteries"])
    job_codes: list[str] | None = Field(None, description="Job codes from /jobs, e.g. ['BATTERY']")
    include_parts: bool = Field(True, description="False when the customer supplies the parts - labour is unchanged")
    division: str | None = Field(None, description="Override the division (contract type) if the model is new")
    match: Literal["auto", "context", "job"] = Field(
        "auto", description="context = quote from past segments whose description means the same thing; "
                            "job = only use the fixed job list; auto = context when past work matches well, else the job list")

    @model_validator(mode="after")
    def need_job(self):
        if not self.job_text and not self.job_codes:
            raise ValueError("give job_text or job_codes")
        return self


class ActualRequest(BaseModel):
    actual_hours: float = Field(..., ge=0)
    notes: str | None = None


# ---------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    e = state.engine
    return {"status": "ok" if e else "no_data", "detail": state.error,
            "data": None if not e else {k: e.meta.get(k) for k in
                                        ("built_at", "segments", "eligible_segments", "data_from", "data_to")}}


@app.get("/meta")
def meta():
    return engine().meta


@app.get("/jobs")
def jobs(q: str = "", limit: int = Query(15, le=100)):
    return engine().search_jobs(q, limit)


@app.get("/makes")
def makes():
    return engine().makes()[:200]


@app.get("/models")
def models(make: str | None = None, q: str = "", limit: int = Query(20, le=200)):
    return engine().search_models(make, q, limit)


@app.post("/quotes")
def create_quote(req: QuoteRequest):
    """Quote labour + parts.

    Free text is understood two ways: the fixed job list (battery, starter, PM_500 ...) and **context
    search** over every past segment description ('VALVE LASH ADJUSTMENT' finds 'SET ENGINE VALVES',
    'ADJUST VALVE CLEARANCE', 'AJUSTEMENT DES VALVES' ...)."""
    e = engine()
    m = e.machine(req.make, req.model, req.division)
    codes = [c.strip().upper() for c in (req.job_codes or []) if c.strip()]
    unknown = [c for c in codes if c not in e.catalog]
    if unknown:
        raise HTTPException(422, {"error": f"unknown job codes: {unknown}", "hint": "see GET /jobs"})
    text = (req.job_text or "").strip()
    if not codes and text and req.match != "context":
        codes = e.resolve_jobs(text)

    result = None
    ctx = e.similar_quote(text, m, include_parts=req.include_parts) if text else None
    strong = bool(ctx and ctx["matched_descriptions"] and ctx["matched_descriptions"][0]["score"] >= 0.7)
    if ctx and req.match != "job" and (req.match == "context" or strong or not codes):
        # context first: it backtested slightly better than the job list and covers every job
        result = ctx
        if codes:
            j = e.quote(req.make, req.model, codes, include_parts=False, division=req.division)
            if j["total"]:
                result["job_list_check"] = {"jobs": codes, "total": j["total"],
                                            "display": " + ".join(l["display_name"] for l in j["labor"])}
    elif codes and req.match != "context":
        result = e.quote(req.make, req.model, codes, include_parts=req.include_parts, division=req.division)
        result["mode"] = "job_catalog"
        if ctx:
            result["context_check"] = {"total": ctx["total"], "matched_segments": ctx["matched_segments"],
                                       "level_used": ctx["labor"][0]["level_used"],
                                       "sample_size": ctx["labor"][0]["sample_size"],
                                       "matched_descriptions": ctx["matched_descriptions"][:5]}

    if result is None:
        diag = e.diagnose(text, m)
        raise HTTPException(422, {
            "error": f"no past work matches '{text}'" if req.match != "job" else f"'{text}' is not in the job list",
            "suggestions": e.search_jobs(text, 5),
            "diagnosis": diag if diag["likely_fixes"] else None,
            "hint": "try other wording, pick a job from suggestions, or use match='context'",
        })
    if text:
        diag = e.diagnose(text, m)
        if diag["matched_symptoms"] and diag["likely_fixes"]:
            result["diagnosis"] = diag
            result["warnings"].append(f"'{text}' reads like a symptom ({', '.join(diag['matched_symptoms'])}): "
                                      "hours below are for this kind of call; see the likely fixes.")
    result["quote_id"] = state.store.save(req.model_dump(), result)
    return result


@app.get("/similar")
def similar(q: str, make: str, model: str, include_parts: bool = True):
    """Context search only: past segments whose description means the same as `q`, with hours and parts."""
    e = engine()
    r = e.similar_quote(q, e.machine(make, model), include_parts=include_parts)
    if not r:
        raise HTTPException(404, f"no past work matches '{q}'")
    return r


@app.get("/quotes/{quote_id}")
def get_quote(quote_id: str):
    q = state.store.get(quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    return q


@app.post("/quotes/{quote_id}/actual")
def record_actual(quote_id: str, body: ActualRequest):
    if not state.store.record_actual(quote_id, body.actual_hours, body.notes):
        raise HTTPException(404, "quote not found")
    return {"quote_id": quote_id, "recorded": True}


@app.get("/accuracy")
def accuracy():
    return state.store.accuracy()


@app.get("/diagnose")
def diagnose(symptom: str, make: str | None = None, model: str | None = None):
    e = engine()
    m = e.machine(make, model) if make and model else None
    return e.diagnose(symptom, m)


@app.post("/admin/reload")
def reload():
    """Reload the statistics database after re-running the pipeline."""
    load_engine()
    return health()
