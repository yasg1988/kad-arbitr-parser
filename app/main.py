import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.database import init_db, close_db, get_cached_cases, save_cases, get_stats
from app.parser import search_cases_by_inn
from app.models import SearchResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error("Failed to connect to database: %s", e)
    yield
    await close_db()


app = FastAPI(
    title="Kad Arbitr Parser",
    description="REST API for searching Russian arbitration court cases by INN",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def health():
    return {"status": "ok", "service": "kad-arbitr-parser"}


@app.get("/cases/inn/{inn}", response_model=SearchResponse)
async def cases_by_inn(
    inn: str,
    force: bool = Query(False, description="Force fresh search, bypass cache"),
    role: str | None = Query(None, description="Filter by role: plaintiff or respondent"),
):
    if not re.match(r"^\d{10}$|^\d{12}$", inn):
        raise HTTPException(status_code=400, detail="INN must be 10 or 12 digits")

    if role and role not in ("plaintiff", "respondent"):
        raise HTTPException(status_code=400, detail="role must be 'plaintiff' or 'respondent'")

    # Check cache
    cached = await get_cached_cases(inn, force=force)
    if cached:
        logger.info("Cache hit for INN %s (%d cases)", inn, cached.total_cases)
        if role:
            cached.cases = _filter_by_role(cached.cases, inn, role)
            cached.total_cases = len(cached.cases)
        return cached

    # Fetch from kad.arbitr.ru
    logger.info("Fetching cases for INN %s from kad.arbitr.ru", inn)
    cases = await search_cases_by_inn(inn)

    if cases is None:
        raise HTTPException(status_code=502, detail="Failed to fetch data from kad.arbitr.ru")

    # Save to cache
    try:
        await save_cases(inn, cases)
    except Exception as e:
        logger.error("Failed to save cases: %s", e)

    response = SearchResponse(
        inn=inn,
        total_cases=len(cases),
        cases=cases,
    )

    if role:
        response.cases = _filter_by_role(response.cases, inn, role)
        response.total_cases = len(response.cases)

    return response


def _filter_by_role(cases, inn, role):
    """Filter cases where INN appears as plaintiff or respondent."""
    filtered = []
    for case in cases:
        if role == "plaintiff":
            if any(p.inn == inn for p in case.plaintiffs):
                filtered.append(case)
        elif role == "respondent":
            if any(r.inn == inn for r in case.respondents):
                filtered.append(case)
    return filtered


@app.get("/stats")
async def stats():
    return await get_stats()
