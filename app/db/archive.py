"""Postgres archive for job search results.

Every scrape that comes back from JobSpy is written here, keyed on the site and
the site's own posting id, so the same posting seen twice is stored once and a
report can say what is new, still open or gone since a given date.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql+psycopg2://jobspy:jobspy@localhost:5432/jobspy"

metadata = MetaData()

archived_postings = Table(
    "archived_postings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("site", String(64), nullable=False),
    Column("external_id", String(128), nullable=False),
    Column("title", String(512), nullable=True),
    Column("company", String(512), nullable=True),
    Column("location", String(512), nullable=True),
    Column("job_url", Text, nullable=True),
    Column("search_term", String(256), nullable=True),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
    Column("seen_count", Integer, nullable=False, default=1),
    Index("ix_archived_postings_site_external", "site", "external_id", unique=True),
    Index("ix_archived_postings_last_seen", "last_seen"),
)

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def get_database_url() -> str:
    """Read the archive database URL from the environment."""
    return os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL


def get_engine(url: Optional[str] = None) -> Engine:
    """Return a process-wide engine, creating it on first use."""
    global _engine, _session_factory
    if url is not None:
        engine = create_engine(url, future=True, pool_pre_ping=True)
        return engine
    if _engine is None:
        _engine = create_engine(get_database_url(), future=True, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session(engine: Optional[Engine] = None) -> Session:
    """Open a session on the archive engine (or the given one)."""
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    get_engine()
    assert _session_factory is not None
    return _session_factory()


def init_archive(engine: Optional[Engine] = None) -> None:
    """Create the archive table when it is missing."""
    metadata.create_all(engine or get_engine())


def _normalise(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN from pandas
        return None
    text = str(value).strip()
    return text or None


def _row_to_record(row: Dict[str, Any], search_term: Optional[str]) -> Optional[Dict[str, Any]]:
    site = _normalise(row.get("site"))
    external_id = _normalise(row.get("id"))
    if not site or not external_id:
        return None
    return {
        "site": site.lower(),
        "external_id": external_id,
        "title": _normalise(row.get("title")),
        "company": _normalise(row.get("company")),
        "location": _normalise(row.get("location")),
        "job_url": _normalise(row.get("job_url")),
        "search_term": _normalise(search_term),
    }


def upsert_postings(
    rows: Iterable[Dict[str, Any]],
    search_term: Optional[str] = None,
    engine: Optional[Engine] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Write scraped rows into the archive, deduplicated on site and external id.

    Returns counts of inserted and refreshed postings.
    """
    seen_at = now or datetime.now(timezone.utc)
    records: List[Dict[str, Any]] = []
    for row in rows:
        record = _row_to_record(row, search_term)
        if record is not None:
            records.append(record)

    if not records:
        return {"inserted": 0, "refreshed": 0}

    target_engine = engine or get_engine()
    init_archive(target_engine)
    dialect = target_engine.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert

    inserted = 0
    refreshed = 0
    with get_session(target_engine) as session:
        for record in records:
            stmt = insert_fn(archived_postings).values(
                **record,
                first_seen=seen_at,
                last_seen=seen_at,
                seen_count=1,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["site", "external_id"],
                set_={
                    "last_seen": seen_at,
                    "seen_count": archived_postings.c.seen_count + 1,
                    "title": stmt.excluded.title,
                    "company": stmt.excluded.company,
                    "location": stmt.excluded.location,
                    "job_url": stmt.excluded.job_url,
                },
            ).returning(archived_postings.c.seen_count)
            seen_count = session.execute(stmt).scalar_one()
            if seen_count == 1:
                inserted += 1
            else:
                refreshed += 1
        session.commit()

    logger.info("Archived %s new and %s refreshed postings", inserted, refreshed)
    return {"inserted": inserted, "refreshed": refreshed}


def changes_since(
    since: datetime,
    engine: Optional[Engine] = None,
    limit: int = 500,
) -> Dict[str, List[Dict[str, Any]]]:
    """Report postings that are new, still open or gone since a given moment."""
    target_engine = engine or get_engine()
    init_archive(target_engine)
    with get_session(target_engine) as session:
        new_rows = session.execute(
            select(archived_postings)
            .where(archived_postings.c.first_seen >= since)
            .order_by(archived_postings.c.first_seen.desc())
            .limit(limit)
        ).mappings().all()
        open_rows = session.execute(
            select(archived_postings)
            .where(archived_postings.c.first_seen < since)
            .where(archived_postings.c.last_seen >= since)
            .order_by(archived_postings.c.last_seen.desc())
            .limit(limit)
        ).mappings().all()
        gone_rows = session.execute(
            select(archived_postings)
            .where(archived_postings.c.last_seen < since)
            .order_by(archived_postings.c.last_seen.desc())
            .limit(limit)
        ).mappings().all()

    def serialise(rows) -> List[Dict[str, Any]]:
        return [
            {
                "site": row["site"],
                "external_id": row["external_id"],
                "title": row["title"],
                "company": row["company"],
                "location": row["location"],
                "job_url": row["job_url"],
                "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
                "seen_count": row["seen_count"],
            }
            for row in rows
        ]

    return {
        "new": serialise(new_rows),
        "still_open": serialise(open_rows),
        "gone": serialise(gone_rows),
    }


def archive_stats(engine: Optional[Engine] = None) -> Dict[str, Any]:
    """Totals for the archive: postings, sites and the last write."""
    target_engine = engine or get_engine()
    init_archive(target_engine)
    with get_session(target_engine) as session:
        total = session.execute(select(func.count()).select_from(archived_postings)).scalar_one()
        sites = session.execute(
            select(archived_postings.c.site, func.count())
            .group_by(archived_postings.c.site)
            .order_by(func.count().desc())
        ).all()
        last_seen = session.execute(select(func.max(archived_postings.c.last_seen))).scalar_one()
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recent = session.execute(
            select(func.count())
            .select_from(archived_postings)
            .where(archived_postings.c.first_seen >= week_ago)
        ).scalar_one()
    return {
        "total_postings": total,
        "new_last_7_days": recent,
        "by_site": {site: count for site, count in sites},
        "last_seen": last_seen.isoformat() if last_seen else None,
    }
