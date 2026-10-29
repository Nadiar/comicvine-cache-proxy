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
- **Background jobs** — hourly incremental sync and backfills, daily prefetch/exports/maintenance, with a persistent run history
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
| `BACKUP_DIR` | `/data/backups` | Where nightly database snapshots are written |
| `BACKUP_KEEP` | `3` | Snapshots to keep (`0` disables backups) |
| `CV_DB_EXPORT_PATH` | *(unset)* | Write a ComicManager-compatible `comicvine.db` here daily; unset disables the export |
| `MCL_EXPORT_PATH` | *(unset)* | Keep a daily copy of the community `latest.mcl` here; unset disables it |
| `COVER_HASH_PER_HOUR` | `0` | Cover images hashed per hour into `comic_covers` for ComicManager cover matching (CDN downloads, not API calls); `0` disables |
| `MCL_SOURCE_URL` | Gitea `Update Missing` folder (API listing) | Where to look for the newest `*_latest.mcl` |
| `RATE_LIMIT_PER_MINUTE` | `100` | Global upstream calls per minute |
| `RATE_LIMIT_PER_HOUR_PER_ENDPOINT` | `180` | Upstream calls per hour per CV endpoint path |
| `RATE_STATE_PATH` | `/data/rate_state.json` | Recent upstream call times and cooldown, kept across restarts |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `600` | Pause on all upstream calls after CV answers 420/429/107 (an upstream `Retry-After` wins) |
| `SYNC_ENABLED` | `true` | Enable background scheduler |
| `SYNC_CRON_HOUR` | `3` | Hour (container time zone) for the daily jobs |
| `JOBS_DB_PATH` | next to `STATS_DB_PATH` | Job run history |
| `SEARCH_MAX_PAGES` | `5` | Max upstream pages fetched per search query (100 results/page). A cold search returns after the pages covering the request; the rest are fetched in the background. |
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

Every background job, scheduled or manual, runs through one job runner and is
recorded in `jobs.db` (next to `stats.db`, kept 30 days) with its trigger,
parameters, status, stop reason, summary, duration, result and the log lines
it wrote. The dashboard's Jobs tab shows the catalog, a backlog snapshot and
the run history.

Statuses: `done`, `partial` (stopped early: budget, rate limit, upstream
error...; the next run continues), `idle` (nothing to do), `skipped`
(disabled, previous run still going, missed start), `error`, `cancelled`.

| Endpoint | Description |
|----------|-------------|
| `GET /admin/jobs/catalog` | Every job: description, schedule, next run, parameters, last run, last good run |
| `POST /admin/jobs/{job}/run` | Start a job now; JSON body holds its parameters (see the catalog) |
| `GET /admin/jobs` | Runs, newest first (`?job=`, `?status=`, `?trigger=schedule\|manual`, `?limit=`, `?offset=`) |
| `GET /admin/jobs/{run_id}` | One run, including its log lines |
| `DELETE /admin/jobs/{run_id}` | Cancel a running run (not for jobs that work on a worker thread) |
| `GET /admin/scheduler/history` | The last 100 scheduled runs |

Older per-job endpoints still work and start the same jobs:
`POST /admin/sync/issues?days=`, `/admin/repair/publishers`, `/admin/evict?dry_run=`,
`/admin/cleanup/images`, `/admin/rebuild/fts`, `/admin/export/comicvine-db`,
`/admin/mcl/refresh`, `/admin/mcl/backfill`.

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

When `SYNC_ENABLED=true`, these jobs run on a schedule (times are in the
container's time zone; `H` = `SYNC_CRON_HOUR`). Manual-only jobs are listed
after them. Every job can be started from the Jobs tab.

| Job | Schedule | Description |
| --- | --- | --- |
| Incremental issue sync | Hourly at :10 | Issues changed since the stored watermark, in 24h slices; the watermark only moves over stored data |
| Releases prefetch | Daily at H:00 | Issues with a store date from 2 weeks ago to 1 week ahead |
| Pending fetch | Every 2 min | Cache misses queued while ComicVine couldn't be asked; idle runs aren't kept in history |
| Issue detail backfill | Hourly at :05 | /issue/ details (credits) for issues stored from list responses, newest first |
| Issue backfill | Hourly at :45 | Issues cached by older versions without the newer fields |
| Volume backfill | Hourly at :30 | Volumes missing detail fields (calendar-relevant first) |
| Publisher backfill | Every 6h at :15 | Publishers missing detail fields |
| Search cache volume backfill | Every 2h at :50 | Volumes seen in cached searches but not stored |
| MCL gap backfill | Daily at H+6 | Volumes and issues in `latest.mcl` but missing locally, 100 IDs per request |
| Cover hash backfill | Hourly at :20 | Only when `COVER_HASH_PER_HOUR` > 0 |
| comicvine.db export | Daily at H+5 | Only when `CV_DB_EXPORT_PATH` is set; see below |
| latest.mcl refresh | Daily at H+5:30 | Only when `MCL_EXPORT_PATH` is set |
| WAL checkpoint | Hourly at :40 | PASSIVE checkpoint on its own connection |
| Cache eviction | Daily at H+3 | When eviction is configured; manual runs default to a dry run |
| Image cache cleanup | Daily at H+2 | Images not accessed within `IMAGE_CACHE_TTL_DAYS` |
| Database backup | Daily at H+4 | Only when `BACKUP_KEEP` > 0 |
| Backlog snapshot | Daily at H+2:30 | Read-only counts of every backfill's remaining work (Backlog card) |
| Response cache audit | Weekly, Sat H+1:30 | Reports cached error envelopes, unparseable rows and empty results |
| Issue re-sync (time window) | Manual | Issues changed in the last N days; doesn't move the watermark |
| Re-fetch issues | Manual | Specific issue IDs and/or a volume's issues, optional /issue/ details |
| Response cache audit (purge) | Manual | Deletes error envelopes and unparseable rows |
| Integrity check | Manual | `PRAGMA quick_check` (reads the whole file) |
| Publisher repair | Manual | Canonical publisher IDs for cached volumes |
| FTS rebuild | Manual | Rebuilds the full-text indexes |

Backfill jobs share a per-endpoint budget (120 calls/h), pace their calls across
the hour, yield to queued client requests and stop at the first ComicVine
refusal, leaving headroom for live proxy traffic.

## comicvine.db Export (ComicManager)

With `CV_DB_EXPORT_PATH` set, a daily job writes a `comicvine.db` in the layout of the `localcv.db` distribution that ComicManager reads (and `sqlite_cv_updater.py` maintains):

- `cv_publisher`, `cv_volume`, `cv_issue`, `comic_covers`, plus the `volume_fts` search index
- empty credit lists written as `[]`
- `count_of_issues` is never lower than the number of stored issues

The file is built as `comicvine.db.partial` on local disk next to cvproxy's own database, copied into the destination folder (which can be a network share), and renamed into place. ComicManager opens it with `immutable=1`, so it must never see the file change. Consumers should **mount the directory, not the file**. A single-file bind mount keeps showing the old file after the rename.

`MCL_EXPORT_PATH` (usually the same folder, as `latest.mcl`) keeps a copy of the community missing-comics list. ComicManager only reads it as a fallback when `comicvine.db` is missing.

```yaml
# cvproxy
environment:
  - CV_DB_EXPORT_PATH=/data/export/comicvine.db
  - MCL_EXPORT_PATH=/data/export/latest.mcl
# ComicManager (web and worker)
volumes:
  - /path/to/cvproxy/data/export:/cv_db:ro
```

Limits:

- **Cover hashes:** with `COVER_HASH_PER_HOUR` set, cvproxy adds `comic_covers` rows for newer issues. It uses the `imagehash` pHash and average hash that ComicManager computes, applied to the `scale_medium` image, which hashes the same as the original. Issues whose details haven't been fetched only get their main cover, not variants. Without it, only the hashes imported with the original `localcv.db` exist.
- **Credits:** issues stored only from list responses show empty credits until the issue detail backfill reaches them.

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
