from datetime import datetime

from pydantic import BaseModel


class CaseParty(BaseModel):
    name: str | None = None
    inn: str | None = None
    address: str | None = None


class ArbitrationCase(BaseModel):
    case_id: str
    case_number: str
    case_type: str | None = None
    case_url: str
    court: str | None = None
    judge: str | None = None
    entry_date: str | None = None
    result_date: str | None = None
    decision: str | None = None
    plaintiffs: list[CaseParty] = []
    respondents: list[CaseParty] = []


class SearchResponse(BaseModel):
    inn: str
    total_cases: int
    cases: list[ArbitrationCase]
    cached: bool = False
    cached_at: datetime | None = None


class StatsResponse(BaseModel):
    total_cached_searches: int = 0
    total_cached_cases: int = 0
    oldest_entry: datetime | None = None
    newest_entry: datetime | None = None
