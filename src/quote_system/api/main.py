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
    e = engine()
    codes = [c.strip().upper() for c in (req.job_codes or []) if c.strip()]
    unknown = [c for c in codes if c not in e.catalog]
    if unknown:
        raise HTTPException(422, {"error": f"unknown job codes: {unknown}", "hint": "see GET /jobs"})
    if not codes and req.job_text:
        codes = e.resolve_jobs(req.job_text)
    if not codes:
        diag = e.diagnose(req.job_text or "", e.machine(req.make, req.model, req.division))
        raise HTTPException(422, {
            "error": f"could not match '{req.job_text}' to a job",
            "suggestions": e.search_jobs(req.job_text or "", 5),
            "diagnosis": diag if diag["likely_fixes"] else None,
            "hint": "pick a job_code from suggestions, or if this is a symptom use the likely fixes",
        })
    result = e.quote(req.make, req.model, codes, include_parts=req.include_parts, division=req.division)
    if req.job_text:
        syms = e.diagnose(req.job_text, None)["matched_symptoms"]
        if syms:
            result["warnings"].append(f"'{req.job_text}' also reads like a symptom ({', '.join(syms)}); "
                                      "see /diagnose for what usually fixes it.")
    result["quote_id"] = state.store.save(req.model_dump(), result)
    return result


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
