"""Routes for the Postgres archive of job search results."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import archive
from app.middleware.api_key_auth import get_api_key
from app.services.job_service import JobService

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_since(since: Optional[str]) -> datetime:
    if not since:
        return datetime.now(timezone.utc) - timedelta(days=7)
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid since value",
                "message": "Use an ISO 8601 date such as 2026-10-01 or 2026-10-01T08:00:00Z",
            },
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/archive/changes", dependencies=[Depends(get_api_key)])
async def archive_changes(
    since: Optional[str] = Query(None, description="ISO 8601 date to compare against"),
    limit: int = Query(500, ge=1, le=2000, description="Maximum postings per group"),
) -> Dict[str, Any]:
    """Postings that are new, still open or gone since a given date."""
    since_dt = _parse_since(since)
    try:
        report = archive.changes_since(since_dt, limit=limit)
    except Exception as exc:  # database unreachable or table missing
        logger.exception("Archive change report failed")
        raise HTTPException(
            status_code=503,
            detail={"error": "Archive unavailable", "message": str(exc)},
        ) from exc
    return {
        "since": since_dt.isoformat(),
        "counts": {key: len(value) for key, value in report.items()},
        **report,
    }


@router.get("/archive/stats", dependencies=[Depends(get_api_key)])
async def archive_summary() -> Dict[str, Any]:
    """Totals for the archive, by site and for the last week."""
    try:
        return archive.archive_stats()
    except Exception as exc:
        logger.exception("Archive stats failed")
        raise HTTPException(
            status_code=503,
            detail={"error": "Archive unavailable", "message": str(exc)},
        ) from exc


@router.post("/archive/backfill", dependencies=[Depends(get_api_key)])
async def archive_backfill(
    search_term: str = Query(..., description="Search term to run and archive"),
    location: Optional[str] = Query(None, description="Job location"),
    site_name: Optional[str] = Query(None, description="Comma separated job sites"),
    results_wanted: int = Query(20, ge=1, le=200, description="Results per site"),
) -> Dict[str, Any]:
    """Run one search and write its results into the archive."""
    params: Dict[str, Any] = {
        "search_term": search_term,
        "location": location,
        "results_wanted": results_wanted,
    }
    if site_name:
        params["site_name"] = [s.strip() for s in site_name.split(",") if s.strip()]
    try:
        jobs_df, cached = JobService.search_jobs(params)
        rows = jobs_df.to_dict("records") if jobs_df is not None and not jobs_df.empty else []
        written = archive.upsert_postings(rows, search_term=search_term)
    except Exception as exc:
        logger.exception("Archive backfill failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "Backfill failed", "message": str(exc)},
        ) from exc
    return {"search_term": search_term, "cached": cached, "scraped": len(rows), **written}
