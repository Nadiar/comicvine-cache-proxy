# ComicVine Cache Proxy

ComicVine API proxy with local SQLite cache, full-text search, and on-demand image caching.

Drop-in replacement for the ComicVine API URL — point your tools (ComicTagger, Mylar, etc.) at this proxy instead of `comicvine.gamespot.com`.

## Features

- **CV-compatible REST API** — transparent proxy, tools need no modification
- **Local SQLite cache** — serves cached metadata instantly with no upstream calls for known data
- **Full-text search** — FTS5-powered volume/issue/publisher search with LIKE fallback
- **Smart search cache** — full result pages cached; superset lookup (a cached "batman" query serves "absolute batman" without a new upstream call)
- **On-demand image cache** — images fetched on first request, cached with sliding TTL
- **Rate-limited upstream** — global per-minute limiter + per-endpoint hourly sliding window, matches CV's 200 req/hr limit
- **Background scheduler** — nightly release prefetch, hourly entity backfill, daily incremental sync, periodic cache eviction
- **XML format support** — responds with CV-compatible XML when `format=xml` is requested
- **Stats dashboard** — live cache hit rate, per-client and per-endpoint breakdowns
- **Admin API** — on-demand sync, publisher repair, cache eviction, FTS rebuild
- **Docker-ready** — single container deployment

## Quick Start

### Docker Compose (recommended)

```bash
mkdir -p ./data
CV_API_KEY=your_key docker compose up -d
```

The included `docker-compose.yml` has every setting pre-populated with its default and a comment explaining what it does — uncomment and adjust what you need.

### Local Development

```bash
pip install -e ".[dev]"
CV_API_KEY=your_key cvproxy
```

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CV_API_KEY` | *required* | ComicVine API key |
| `CV_API_BASE_URL` | `https://comicvine.gamespot.com/api` | Upstream CV API base URL |
| `DB_PATH` | `/data/localcv.db` | SQLite cache database path |
| `IMAGE_CACHE_DIR` | `/data/images` | Disk image cache directory |
| `IMAGE_CACHE_TTL_DAYS` | `14` | Image cache sliding TTL (days) |
| `STATS_DB_PATH` | `/data/stats.db` | Request stats database path |
| `RATE_LIMIT_PER_MINUTE` | `100` | Global upstream calls per minute |
| `RATE_LIMIT_PER_HOUR_PER_ENDPOINT` | `180` | Upstream calls per hour per CV endpoint path |
| `SYNC_ENABLED` | `true` | Enable background scheduler |
| `SYNC_CRON_HOUR` | `3` | UTC hour for daily sync jobs |
| `SEARCH_MAX_PAGES` | `5` | Max upstream pages fetched per search query (100 results/page) |
| `RESPONSE_CACHE_TTL_DAYS` | `30` | TTL for generic resource cache; `0` = keep forever |
| `SEARCH_CACHE_TTL_DAYS` | `60` | TTL for search result cache; `0` = keep forever |
| `CACHE_CUTOFF_YEAR` | `0` | Skip caching issues/volumes older than this publication year (e.g. `1995`). Issues checked against `store_date` then `cover_date`; volumes against `last_issue.cover_date`. `0` = disabled. |
| `EVICT_OLDER_THAN_YEARS` | `0` | Evict issues older than N years (disabled when `0`) |
| `EVICT_UNACCESSED_DAYS` | `180` | Grace period: skip eviction for recently-accessed entities |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8585` | Listen port |

## Usage

Point your tools at the proxy instead of the upstream API:

```
# Instead of:
https://comicvine.gamespot.com/api/

# Use:
http://your-server:8585/api/
```

## API Endpoints

### CV-compatible (pass-through)

| Endpoint | Description |
|----------|-------------|
| `GET /api/search/` | Search volumes, issues, publishers |
| `GET /api/volume/4050-{id}/` | Volume detail |
| `GET /api/volumes/` | List/filter volumes |
| `GET /api/issue/4000-{id}/` | Issue detail |
| `GET /api/issues/` | List/filter issues |
| `GET /api/publisher/4010-{id}/` | Publisher detail |
| `GET /api/publishers/` | List/filter publishers |
| `GET /api/{resource}/{id}/` | Generic single resource (characters, teams, story arcs, etc.) |
| `GET /api/{resources}/` | Generic list resource |
| `GET /api/image/...` | Cached image proxy |

### Utility

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check with DB counts and image cache stats |
| `GET /dashboard` | HTML dashboard (cache hit rate, per-client stats) |
| `GET /dashboard/data` | Dashboard JSON data (`?hours=N` to filter to last N hours) |
| `GET /docs` | Interactive OpenAPI docs |

### Admin

All admin endpoints return immediately with a `job_id`. Poll `GET /admin/jobs/{job_id}` for status.

| Endpoint | Description |
|----------|-------------|
| `POST /admin/sync/issues` | Re-sync issues updated in the last N days (`?days=14`) |
| `POST /admin/repair/publishers` | Re-fetch publisher IDs for cached volumes |
| `POST /admin/evict` | Evict stale data (`?dry_run=true` previews without deleting) |
| `POST /admin/cleanup/images` | Remove expired image cache entries immediately |
| `POST /admin/rebuild/fts` | Rebuild FTS5 search indexes |
| `GET /admin/jobs` | List all background jobs |
| `GET /admin/jobs/{id}` | Poll job status |
| `DELETE /admin/jobs/{id}` | Cancel a running job |

## Cache Control

All `/api/` endpoints support the `X-CV-Cache` request header:

| Value | Behaviour |
|-------|-----------|
| *(absent)* | Serve from local cache; fall through to upstream on miss |
| `only-if-cached` | Local cache only — no upstream call, no rate-limit impact. Returns `status_code: 101` on miss for detail endpoints, empty results for list endpoints. |
| `no-cache` | Skip local cache; force upstream fetch and backfill |

```http
# Cache-only lookup (bulk scraper operations)
GET /api/issue/4000-1073108/?api_key=... HTTP/1.1
X-CV-Cache: only-if-cached

# Force refresh
GET /api/volume/4050-160294/?api_key=... HTTP/1.1
X-CV-Cache: no-cache
```

## Limiting What Gets Cached

### Ingest cutoff (`CACHE_CUTOFF_YEAR`)

Set this to a four-digit year (e.g. `1990`) to silently skip writing any issue or volume older than that year. Content is checked at write time — nothing is deleted from an existing database, and the filter does not affect upstream proxy responses (old data is still forwarded to clients, just not stored locally).

- Issues: checked against `store_date`, falls back to `cover_date`.
- Volumes: checked against `last_issue.cover_date` when present; volumes without that field are cached regardless.

### Cache eviction (`EVICT_OLDER_THAN_YEARS`)

Eviction removes rows that are *already in the database*. By default it is disabled (`EVICT_OLDER_THAN_YEARS=0`). When configured:

- **Issues** are removed when their `cover_date` predates the cutoff **and** they haven't been accessed within `EVICT_UNACCESSED_DAYS`.
- **Volumes** are cascade-removed when they have no remaining issues and are also unaccessed.
- **Response/search caches** expire independently via `RESPONSE_CACHE_TTL_DAYS` / `SEARCH_CACHE_TTL_DAYS`.

Use `POST /admin/evict?dry_run=true` (or the dashboard Jobs tab) to preview candidate counts before committing.

## Background Scheduler

When `SYNC_ENABLED=true`, the following jobs run automatically:

| Job | Schedule | Description |
| --- | --- | --- |
| Releases prefetch | Daily at `SYNC_CRON_HOUR` | Fetches issues with `store_date` in a ±2 week window |
| Incremental issue sync | Daily, 1h after prefetch | Re-fetches issues updated in the last 48h |
| Volume backfill | Hourly at :30 | Re-fetches volumes missing new fields |
| Issue backfill | Hourly at :45 | Re-fetches issues missing new fields |
| Publisher backfill | Every 6h at :15 | Re-fetches publishers missing new fields |
| Search cache backfill | Every 2h at :50 | Fetches volumes seen in search results but not yet in DB |
| Stale data eviction | Daily, 3h after prefetch | Removes expired entities (when eviction is configured) |
| Image cache cleanup | Daily, 2h after prefetch | Removes expired image cache entries |

Backfill jobs are rate-paced to stay within the hourly CV API budget, leaving headroom for live proxy traffic.

## Development

```bash
# Run tests
pytest -v

# Lint and format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src --strict
```
