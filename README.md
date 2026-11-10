# JobSpy Postgres Archive

A FastAPI service that runs job searches and keeps every result in Postgres so postings can be compared over time.

```mermaid
flowchart LR
    client["Client"] --> api["FastAPI routes"]
    api --> svc["Job service"]
    svc --> spy["JobSpy scraper"]
    spy --> svc
    svc --> cache[("In-memory cache")]
    svc --> arch["Archive module"]
    arch --> db[("Postgres")]
    api --> arch
    arch --> api
```

## Why

Job searches are usually one-shot: you run a query, read the results, and the next run starts from nothing. That makes it hard to tell whether a posting is genuinely new, has been reposted for weeks, or has quietly disappeared. The usual fix is a spreadsheet or a saved CSV, which drifts out of date and cannot answer "what changed since last Tuesday". This service keeps the history in a database and answers that question directly, for anyone tracking a market or a shortlist of postings.

## How it works

A search request goes through the FastAPI routes, which hand the parameters to the job service. The service calls the JobSpy scraper, caches the result, and writes every row into the archive table. The archive deduplicates on the site name and the site's own posting id, so a posting seen twice is stored once with a refreshed last-seen date. The change report compares the archive against a date and returns what is new, still open or gone.

The archive feature added on top of the base API is the `app/db/archive.py` module and the `app/routes/archive.py` router. The module holds the engine, the session factory and the `archived_postings` table, with `upsert_postings` for writes and `changes_since` for the report. The router exposes `GET /api/v1/archive/changes` and `GET /api/v1/archive/stats`, and the job service calls `upsert_postings` after each fresh scrape.

`upsert_postings` normalises each scraped row, drops rows without a site or a posting id, and writes the rest with an insert that updates on conflict of the site and external id pair, bumping `seen_count` and `last_seen` and returning the new count so the call can report how many postings were inserted and how many refreshed. `changes_since` runs three queries against the same table: rows first seen at or after the date, rows first seen before it but last seen at or after it, and rows last seen before it, each ordered by its own timestamp and capped by the limit. `archive_stats` counts the whole table, groups it by site, and counts postings first seen in the last seven days. The router parses the `since` parameter as ISO 8601, defaulting to seven days back, and turns a database failure into a 503 instead of a stack trace. A third route, `POST /api/v1/archive/backfill`, runs one search through `JobService.search_jobs` and archives its rows, which is how an empty archive gets filled without waiting for the next scheduled search.

## Key decisions

- The cost is that a posting whose id changes is stored as a new row.
- The archive writes happen inside the search request instead of a background worker, so a search and its archive stay consistent. The cost is a slightly slower response when the database is under load.
- The change report compares dates rather than storing an explicit status column, so the archive stays append-friendly and old rows are never rewritten. The cost is that "gone" is inferred from the last-seen date and a configurable window.
- The database connection is configured through one `DATABASE_URL` setting with a local Postgres default, so the same code runs in Docker and on a laptop. The cost is that a missing environment variable silently points at localhost.

## Tech stack

- Python 3.13 with FastAPI and Uvicorn
- PostgreSQL with SQLAlchemy and Alembic
- pandas for the scraped result frames
- python-jobspy for the site scrapers
- Docker and Docker Compose for the service and database

## Quick start

Prerequisites: Python 3.13, Docker, and a Postgres 16 instance or the Compose service.

```bash
pip install -r requirements.txt
make run
```

```bash
docker-compose up -d
```

```bash
pytest --cov=app tests/
```

| Variable | What it sets | Default |
| --- | --- | --- |
| `DATABASE_URL` | SQLAlchemy connection string for the archive | `postgresql+psycopg2://postgres:postgres@localhost:5432/jobspy` |
| `ENABLE_API_KEY_AUTH` | Whether requests must carry the API key header | `false` |
| `API_KEYS` | Comma-separated accepted API keys | empty |
| `RATE_LIMIT_ENABLED` | Whether the rate limiter runs | `false` |
| `RATE_LIMIT_REQUESTS` | Requests allowed per timeframe | `100` |
| `RATE_LIMIT_TIMEFRAME` | Rate limit window in seconds | `3600` |
| `DEFAULT_SITE_NAMES` | Sites searched when none are given | `indeed,linkedin,zip_recruiter,glassdoor,google,bayt,naukri` |
| `DEFAULT_RESULTS_WANTED` | Results per site | `20` |
| `DEFAULT_COUNTRY_INDEED` | Country filter for Indeed and Glassdoor | `USA` |
| `ENABLE_CACHE` | Whether search results are cached | `false` |
| `CACHE_EXPIRY` | Cache lifetime in seconds | `3600` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `CORS_ORIGINS` | Allowed origins | `*` |

## Usage

```bash
curl "http://localhost:8000/api/v1/archive/changes?since=2026-10-01&site_name=indeed"
```

```json
{
  "since": "2026-10-01",
  "new": 214,
  "still_open": 1873,
  "gone": 96,
  "total_archived": 2183
}
```

## Project structure

```text
app/
  db/archive.py        archive table, upserts and change queries
  routes/archive.py    archive endpoints
  routes/api.py        search endpoints
  services/           job service and background helpers
  config.py           environment settings
tests/
  test_archive.py      archive tests on in-memory SQLite
ui/index.html          archive dashboard
```

## Limitations

- The archive stores the fields JobSpy returns; full descriptions are only kept when the scraper fetches them.
- There is no scheduled search runner in the repository, so runs are triggered by requests or an external scheduler.
- The change report treats a posting as gone after a fixed window and does not track edits to a posting's text.
- Alembic migrations are set up for the archive table only; the rest of the schema is created at startup.

## License

MIT. This project builds on [jobspy-api](https://github.com/rainmanjam/jobspy-api) and the [python-jobspy](https://github.com/speedyapply/JobSpy) library.
