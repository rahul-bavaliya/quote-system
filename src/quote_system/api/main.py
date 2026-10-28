"""FastAPI service.

    uvicorn quote_system.api.main:app --reload     (or: make run)

Docs at /docs, the quoting page at /.

Production settings (environment variables, see .env.example):
    QS_API_KEYS      comma-separated keys; when set every API call needs header  X-API-Key
    QS_ADMIN_KEY     key for /admin/* (reload after a new data build)
    QS_CORS_ORIGINS  comma-separated origins allowed to call the API from a browser
    QS_LOG_JSON=1    one JSON log line per request, with request id and latency
"""
from __future__ import annotations

import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import get_settings
from ..engine import EngineNotReady, QuoteEngine, normalize_location
from ..logging_setup import request_id, setup_logging
from .store import QuoteStore

log = logging.getLogger("quote_system.api")
STATIC = Path(__file__).parent / "static"
OPEN_PATHS = {"/", "/health", "/ready", "/version", "/docs", "/openapi.json", "/docs/oauth2-redirect", "/redoc"}


class State:
    engine: QuoteEngine | None = None
    error: str | None = None
    store: QuoteStore | None = None
    api_keys: list[str] = []
    admin_key: str = ""
    loaded_at: float | None = None


state = State()


def load_engine() -> None:
    s = get_settings()
    try:
        new = QuoteEngine(s)
        old, state.engine, state.error = state.engine, new, None
        state.loaded_at = time.time()
        if old:
            old.con.close()
        log.info("statistics loaded from %s (built %s)", s.db_path, new.meta.get("built_at"))
    except EngineNotReady as e:
        state.engine, state.error = None, str(e)
        log.warning(str(e))
    if state.store is None:
        state.store = QuoteStore(s.quotes_db_path)


def check_keys(api_keys: list[str], admin_key: str) -> None:
    """Refuse to start with the placeholder keys from .env.example; warn about short ones."""
    keys = api_keys + ([admin_key] if admin_key else [])
    if any("change-me" in k.lower() for k in keys):
        raise RuntimeError("QS_API_KEYS / QS_ADMIN_KEY still hold the placeholder from .env.example. Generate real keys: "
                           "python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
    if any(len(k) < 16 for k in keys):
        log.warning("an API or admin key is shorter than 16 characters - use long random keys in production")


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    setup_logging(s.log_json, s.log_level)
    state.api_keys = [k.strip() for k in s.api_keys.split(",") if k.strip()]
    state.admin_key = s.admin_key.strip()
    if not state.api_keys:
        log.warning("QS_API_KEYS is not set - the API is open to anyone who can reach it (fine for local dev only)")
    check_keys(state.api_keys, state.admin_key)
    load_engine()
    yield


settings = get_settings()
app = FastAPI(title="Service Quote System", version=settings.version, lifespan=lifespan,
              description="Labour hours (shop and off-site) + parts kits for a job on a make/model, "
                          "learned from historical work orders.")
if settings.cors_origins.strip():
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"])


def _key_ok(given: str | None, valid: list[str]) -> bool:
    return bool(given) and any(hmac.compare_digest(given, k) for k in valid)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    token = request_id.set(rid)
    start = time.perf_counter()
    path = request.url.path
    try:
        if state.api_keys and path not in OPEN_PATHS and not path.startswith("/admin") \
                and not _key_ok(request.headers.get("X-API-Key"), state.api_keys):
            response = JSONResponse({"detail": "missing or invalid X-API-Key", "request_id": rid}, status_code=401)
        else:
            response = await call_next(request)
    except Exception:  # noqa: BLE001 - last line of defence: log it, never leak a stack trace
        log.exception("unhandled error on %s %s", request.method, path)
        response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
    response.headers["X-Request-ID"] = rid
    log.info("%s %s %s", request.method, path, response.status_code,
             extra={"method": request.method, "path": path, "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                    "client": request.client.host if request.client else None})
    request_id.reset(token)
    return response


def engine() -> QuoteEngine:
    if state.engine is None:
        raise HTTPException(503, state.error or "statistics database not built - run `make pipeline`")
    return state.engine


def require_admin(x_admin_key: str | None = Header(None), x_api_key: str | None = Header(None)) -> None:
    if state.admin_key:
        if not _key_ok(x_admin_key, [state.admin_key]):
            raise HTTPException(403, "admin key required (X-Admin-Key)")
    elif state.api_keys and not _key_ok(x_api_key, state.api_keys):
        raise HTTPException(401, "missing or invalid X-API-Key")


# ---------------------------------------------------------------- schemas
class QuoteRequest(BaseModel):
    make: str = Field("", max_length=60, examples=["John Deere"],
                      description="Any spelling (JD, John Deere, Deere ...). May be left blank when only one make has the model.")
    model: str = Field(..., min_length=1, max_length=60, examples=["135G LC"])
    job_text: str | None = Field(None, max_length=300,
                                 description="Free text, e.g. 'replace battery' or 'valve lash adjustment'",
                                 examples=["replace batteries"])
    job_codes: list[str] | None = Field(None, max_length=10, description="Job codes from /jobs, e.g. ['BATTERY']")
    part_numbers: list[str] | None = Field(
        None, max_length=50, description="Quote from the parts alone (no description needed): labour of past jobs "
                                         "that used the same parts. job_text, if given too, narrows the match.",
        examples=[["TY25879B"]])
    include_parts: bool = Field(True, description="False when the customer supplies the parts - labour is unchanged")
    division: str | None = Field(None, max_length=60,
                                 description="Override the division (contract type) if the model is new")
    location: str | None = Field(None, description="'shop' or 'offsite' to quote that location; the response "
                                                   "always includes both under by_location", examples=["shop"])
    match: Literal["auto", "context", "job"] = Field(
        "auto", description="context = quote from past segments whose description means the same thing; "
                            "job = only use the fixed job list; auto = context when past work matches well, else the job list")

    @field_validator("part_numbers")
    @classmethod
    def _parts(cls, v):
        if v and any(len(str(p)) > 40 for p in v):
            raise ValueError("part numbers must be at most 40 characters")
        return v

    @field_validator("location")
    @classmethod
    def _loc(cls, v):
        return normalize_location(v)

    @model_validator(mode="after")
    def need_job(self):
        if not self.job_text and not self.job_codes and not [p for p in (self.part_numbers or []) if str(p).strip()]:
            raise ValueError("give job_text, job_codes or part_numbers")
        return self


class ActualRequest(BaseModel):
    actual_hours: float = Field(..., ge=0, le=2000)
    notes: str | None = Field(None, max_length=500)


# ---------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health", tags=["ops"])
def health():
    """Liveness: the process is up (always 200). Data status is included for humans."""
    e = state.engine
    return {"status": "ok" if e else "no_data", "detail": state.error, "version": settings.version,
            "auth": bool(state.api_keys),
            "data": None if not e else {k: e.meta.get(k) for k in
                                        ("built_at", "segments", "eligible_segments", "data_from", "data_to")}}


@app.get("/ready", tags=["ops"])
def ready():
    """Readiness: 200 only when statistics are loaded (use for load balancer / container health checks)."""
    if state.engine is None:
        return JSONResponse({"ready": False, "detail": state.error}, status_code=503)
    return {"ready": True, "built_at": state.engine.meta.get("built_at")}


@app.get("/version", tags=["ops"])
def version():
    e = state.engine
    return {"version": settings.version, "data_built_at": e.meta.get("built_at") if e else None}


@app.get("/meta", tags=["ops"])
def meta():
    return engine().meta


@app.get("/jobs")
def jobs(q: str = Query("", max_length=100), limit: int = Query(15, ge=1, le=100)):
    return engine().search_jobs(q, limit)


@app.get("/makes")
def makes():
    return engine().makes()[:200]


@app.get("/models")
def models(make: str | None = Query(None, max_length=60), q: str = Query("", max_length=60),
           limit: int = Query(20, ge=1, le=200)):
    return engine().search_models(make, q, limit)


@app.get("/machines/resolve")
def resolve_machine(make: str = Query("", max_length=60), model: str = Query(..., max_length=60)):
    """How a make/model as typed is understood: 'John Deere' '135G LC' -> JD 135GLC, series 135G."""
    return engine().machine(make, model).as_dict()


@app.post("/quotes")
def create_quote(req: QuoteRequest):
    """Quote labour + parts, with shop and off-site hours.

    Free text is understood two ways: the fixed job list (battery, starter, PM_500 ...) and **context
    search** over every past segment description ('VALVE LASH ADJUSTMENT' finds 'SET ENGINE VALVES',
    'ADJUST VALVE CLEARANCE', 'AJUSTEMENT DES VALVES' ...). Part numbers alone also work."""
    e = engine()
    m = e.machine(req.make, req.model, req.division)
    loc = req.location
    codes = [c.strip().upper() for c in (req.job_codes or []) if c.strip()]
    unknown = [c for c in codes if c not in e.catalog]
    if unknown:
        raise HTTPException(422, {"error": f"unknown job codes: {unknown}", "hint": "see GET /jobs"})
    text = (req.job_text or "").strip()
    if not codes and text and req.match != "context":
        codes = e.resolve_jobs(text)

    pns = [str(p).strip() for p in (req.part_numbers or []) if str(p).strip()]
    if pns and not codes:
        result = e.parts_quote(pns, m, include_parts=req.include_parts, text=text or None, location=loc)
        if result is not None:
            result["quote_id"] = state.store.save(req.model_dump(), result)
            return result
        if not text:
            raise HTTPException(422, {"error": f"none of these part numbers are in the parts history: {pns}",
                                      "suggestions": [], "diagnosis": None,
                                      "hint": "check the part numbers (superseded numbers may differ) or add a job description"})

    result = None
    ctx = e.similar_quote(text, m, include_parts=req.include_parts, location=loc) if text else None
    strong = bool(ctx and ctx["matched_descriptions"] and ctx["matched_descriptions"][0]["score"] >= 0.7)
    if ctx and req.match != "job" and (req.match == "context" or strong or not codes):
        # context first: it backtested slightly better than the job list and covers every job
        result = ctx
        if codes:
            j = e.quote(req.make, req.model, codes, include_parts=False, division=req.division, location=loc)
            if j["total"]:
                result["job_list_check"] = {"jobs": codes, "total": j["total"],
                                            "display": " + ".join(lab["display_name"] for lab in j["labor"])}
    elif codes and req.match != "context":
        result = e.quote(req.make, req.model, codes, include_parts=req.include_parts, division=req.division,
                         location=loc)
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


@app.get("/parts/lookup")
def parts_lookup(part_numbers: str = Query(..., max_length=2000)):
    """Check part numbers (comma separated): description, role and how often each appears in history."""
    pns = [p for p in re.split(r"[,\s;]+", part_numbers) if p][:50]
    known, unknown = engine().resolve_parts(pns)
    return {"known": known, "unknown": unknown}


@app.get("/similar")
def similar(q: str = Query(..., max_length=300), make: str = Query("", max_length=60),
            model: str = Query(..., max_length=60), include_parts: bool = True, location: str | None = None):
    """Context search only: past segments whose description means the same as `q`, with hours and parts."""
    e = engine()
    try:
        loc = normalize_location(location)
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from None
    r = e.similar_quote(q, e.machine(make, model), include_parts=include_parts, location=loc)
    if not r:
        raise HTTPException(404, f"no past work matches '{q}'")
    return r


@app.get("/admin/make-entries", dependencies=[Depends(require_admin)], tags=["ops"])
def make_entries(limit: int = Query(200, ge=1, le=2000)):
    """Make spellings typed into quotes that were guessed (word / fuzzy / from_model) or not recognised.

    Review this list now and then: confirm a guess or fix it by adding `alias,canonical` lines to
    config/make_aliases.csv, then rebuild. Each confirmed spelling is then matched exactly."""
    rows = state.store.make_entries(limit)
    return {"entries": rows,
            "make_aliases_csv": [f"{r['entered'].upper()},{r['read_as']}," for r in rows
                                 if r["how"] in ("word", "fuzzy", "from_model") and r["entered"]]}


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
def diagnose(symptom: str = Query(..., max_length=300), make: str | None = Query(None, max_length=60),
             model: str | None = Query(None, max_length=60)):
    e = engine()
    m = e.machine(make, model) if make and model else None
    return e.diagnose(symptom, m)


@app.post("/admin/reload", tags=["ops"], dependencies=[Depends(require_admin)])
def reload():
    """Reload the statistics database after re-running the pipeline (no restart needed)."""
    load_engine()
    return health()
