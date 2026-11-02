# Changelog

## 2.1.0 (2026-11-01)

- **Incremental volume sync:** an hourly job re-reads volumes ComicVine changed since its own watermark, like the issue sync. Before, a volume edited on ComicVine after cvproxy stored it stayed as first stored.
- **Volume sync rewind:** a manual job moves the volume watermark back N days (default 730) so the hourly sync re-reads that span, 45 days per run.
- Volume rows from list responses (`/volumes/`, search, the sync) no longer wipe stored character, people, concept and object lists.
- MCL gap backfill skips blocked publishers' volumes and issues (they were never stored, so they counted as missing every day), and with an allowlist only fetches issues of allowlisted publishers' volumes.
- README: private-use notice (keep servers off the public internet, don't distribute the data, per the ComicVine API terms), plus sections on the local database and the missing comics list.

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
