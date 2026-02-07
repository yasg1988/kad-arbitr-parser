import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from app.config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, CACHE_TTL_HOURS
from app.models import ArbitrationCase, CaseParty, SearchResponse, StatsResponse

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _ensure_pool() -> asyncpg.Pool | None:
    """Lazy pool initialization with retry on each call."""
    global _pool
    if _pool:
        return _pool
    try:
        _pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            min_size=1,
            max_size=5,
        )
        logger.info("Database pool created: %s@%s:%s/%s", DB_USER, DB_HOST, DB_PORT, DB_NAME)
    except Exception as e:
        logger.warning("Database unavailable: %s", e)
        _pool = None
    return _pool


async def init_db() -> None:
    await _ensure_pool()


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_cached_cases(inn: str, force: bool = False) -> SearchResponse | None:
    """Get cached cases for an INN. Returns None if not cached or expired."""
    pool = await _ensure_pool()
    if not pool or force:
        return None

    rows = await pool.fetch(
        "SELECT * FROM arbitration_cases WHERE search_inn = $1 ORDER BY entry_date DESC",
        inn,
    )
    if not rows:
        return None

    # Check TTL on the most recently updated row
    newest_update = max(row["updated_at"] for row in rows)
    if newest_update.tzinfo is None:
        newest_update = newest_update.replace(tzinfo=timezone.utc)

    ttl = timedelta(hours=CACHE_TTL_HOURS)
    if datetime.now(timezone.utc) - newest_update > ttl:
        return None

    cases = []
    for row in rows:
        plaintiffs = [CaseParty(**p) for p in (json.loads(row["plaintiffs"]) if row["plaintiffs"] else [])]
        respondents = [CaseParty(**r) for r in (json.loads(row["respondents"]) if row["respondents"] else [])]
        cases.append(ArbitrationCase(
            case_id=row["case_id"],
            case_number=row["case_number"] or "",
            case_type=row["case_type"],
            case_url=row["case_url"] or "",
            court=row["court"],
            judge=row["judge"],
            entry_date=row["entry_date"],
            result_date=row["result_date"],
            decision=row["decision"],
            plaintiffs=plaintiffs,
            respondents=respondents,
        ))

    return SearchResponse(
        inn=inn,
        total_cases=len(cases),
        cases=cases,
        cached=True,
        cached_at=newest_update,
    )


async def save_cases(inn: str, cases: list[ArbitrationCase]) -> None:
    """Save cases to database. Upserts by (search_inn, case_id)."""
    pool = await _ensure_pool()
    if not pool:
        return

    # Delete old cases for this INN and insert fresh
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM arbitration_cases WHERE search_inn = $1", inn)
            for case in cases:
                plaintiffs_json = json.dumps(
                    [p.model_dump() for p in case.plaintiffs], ensure_ascii=False
                )
                respondents_json = json.dumps(
                    [r.model_dump() for r in case.respondents], ensure_ascii=False
                )
                await conn.execute("""
                    INSERT INTO arbitration_cases (
                        search_inn, case_id, case_number, case_type, case_url,
                        court, judge, entry_date, result_date, decision,
                        plaintiffs, respondents, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW()
                    )
                """, inn, case.case_id, case.case_number, case.case_type,
                    case.case_url, case.court, case.judge, case.entry_date,
                    case.result_date, case.decision, plaintiffs_json, respondents_json)

    logger.info("Saved %d cases for INN %s", len(cases), inn)


async def get_stats() -> StatsResponse:
    pool = await _ensure_pool()
    if not pool:
        return StatsResponse()

    row = await pool.fetchrow("""
        SELECT
            COUNT(DISTINCT search_inn) as total_searches,
            COUNT(*) as total_cases,
            MIN(created_at) as oldest,
            MAX(updated_at) as newest
        FROM arbitration_cases
    """)
    return StatsResponse(
        total_cached_searches=row["total_searches"],
        total_cached_cases=row["total_cases"],
        oldest_entry=row["oldest"],
        newest_entry=row["newest"],
    )
