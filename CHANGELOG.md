# Changelog

## 2.0.0 (2026-11-01)

Breaking changes:

- **Publisher blocklist:** publishers on the blocklist are dropped from every `/api/` response (direct lookups answer `status_code: 101`), never stored, and left out of the `comicvine.db` export. A daily job deletes their stored rows. The first start seeds 175 foreign-language publishers and UK/Australian reprint houses.
- **Publisher allowlist:** the issue, issue detail, cover hash, volume and publisher backfills only fetch allowlisted publishers. The first start seeds 18 major US publishers.
- **Dashboard sign-in:** with `DASHBOARD_PASSWORD` set, `/dashboard`, `/admin/*` and `/docs` need a session. `/api/*` and `/health` stay open.

New:

- Publishers tab: edit both lists, search stored publishers, CSV export and import.
- Settings tab: run-time settings stored in `jobs.db`, taking precedence over the environment.
- Jobs tab: one job runner with persistent run history, per-run logs, stop reasons, descriptions showing the configured values, and "~N left" estimates for the backfills.
- `BACKFILL_MIN_YEAR` floor for proactive backfills (default 2010).
- Cover hashes in `comic_covers` for ComicManager's cover matching.
- `comicvine.db` and `latest.mcl` exports for ComicManager; MCL gap backfill.
- Queue for cache misses that hit a rate limit; persisted rate state; per-endpoint budgets.
- Watermark-based incremental issue sync; nightly database backups; hourly WAL checkpoint.
- Issue detail fetch and backfill for rows stored from list responses.

## 0.1.0

Initial release: ComicVine-compatible proxy with a SQLite cache, FTS search, search cache, image cache, rate limiting and a stats dashboard.
