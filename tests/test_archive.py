"""Tests for the Postgres archive and its change report."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.db import archive


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", future=True)
    archive.init_archive(eng)
    yield eng
    eng.dispose()


def _row(external_id, site="indeed", title="Backend Engineer"):
    return {
        "site": site,
        "id": external_id,
        "title": title,
        "company": "Example Labs",
        "location": "Remote",
        "job_url": f"https://example.com/jobs/{external_id}",
    }


def test_upsert_deduplicates_on_site_and_id(engine):
    first = archive.upsert_postings([_row("a1"), _row("a2")], search_term="python", engine=engine)
    assert first == {"inserted": 2, "refreshed": 0}

    second = archive.upsert_postings([_row("a1"), _row("a3")], search_term="python", engine=engine)
    assert second == {"inserted": 1, "refreshed": 1}

    stats = archive.archive_stats(engine=engine)
    assert stats["total_postings"] == 3
    assert stats["by_site"] == {"indeed": 3}


def test_same_id_on_two_sites_is_two_postings(engine):
    archive.upsert_postings([_row("x9", site="indeed"), _row("x9", site="linkedin")], engine=engine)
    stats = archive.archive_stats(engine=engine)
    assert stats["total_postings"] == 2
    assert set(stats["by_site"]) == {"indeed", "linkedin"}


def test_rows_without_id_are_skipped(engine):
    result = archive.upsert_postings([{"site": "indeed", "title": "No id"}], engine=engine)
    assert result == {"inserted": 0, "refreshed": 0}
    assert archive.archive_stats(engine=engine)["total_postings"] == 0


def test_changes_report_splits_new_open_and_gone(engine):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=20)
    archive.upsert_postings([_row("old-gone")], engine=engine, now=old)
    archive.upsert_postings([_row("old-open")], engine=engine, now=old)
    archive.upsert_postings([_row("old-open")], engine=engine, now=now)
    archive.upsert_postings([_row("fresh")], engine=engine, now=now)

    report = archive.changes_since(now - timedelta(days=7), engine=engine)
    assert [p["external_id"] for p in report["new"]] == ["fresh"]
    assert [p["external_id"] for p in report["still_open"]] == ["old-open"]
    assert [p["external_id"] for p in report["gone"]] == ["old-gone"]
    assert report["still_open"][0]["seen_count"] == 2
