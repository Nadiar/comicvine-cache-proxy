# Changelog

## Unreleased

- **Volume backfill finds list-only volumes:** it picked only volumes without `date_last_updated`, which every list row has, so volumes never fetched from `/volume/` (no character/people lists, stale issue counts and start years) were never backfilled. Volumes now carry a `detail_fetched_at` marker like issues: set by a `/volume/` fetch, cleared when a list row shows ComicVine changed the volume. Existing volumes with detail lists are marked once at startup.
- **`BACKFILL_MIN_YEAR` applies to every backfill:** the volume backfill (volumes with an issue dated in the window), the search cache volume backfill (start year) and the MCL gap backfill's issues (their volume's dates) now follow it, as the issue, detail and cover backfills already did.
- **Syncs follow the same scope:** the incremental issue and volume syncs, the issue re-sync and the releases prefetch only store rows covered by the allowlist and `BACKFILL_MIN_YEAR`, updates to stored rows included. Run summaries count the skipped rows.
- **Search cache volume backfill skips blocked and non-allowlisted publishers.** Blocked volumes are never stored, so they were fetched again every 2 hours.

## 2.0.0 (2026-11-01)

Breaking changes:

- **Publisher blocklist:** publishers on the blocklist are dropped from every `/api/` response (direct lookups answer `status_code: 101`), never stored, and left out of the `comicvine.db` export. A daily job deletes their stored rows. The first start seeds 175 foreign-language publishers and UK/Australian reprint houses.
- **Publisher allowlist:** the issue, issue detail, cover hash, volume and publisher backfills, and the MCL gap backfill's issue fetches, only cover allowlisted publishers. The first start seeds 18 major US publishers.
- **Dashboard sign-in:** with `DASHBOARD_PASSWORD` set, `/dashboard`, `/admin/*` and `/docs` need a session. `/api/*` and `/health` stay open.

New:

- Publishers tab: edit both lists, search stored publishers, CSV export and import.
- Settings tab: run-time settings stored in `jobs.db`, taking precedence over the environment.
- Jobs tab: one job runner with persistent run history, per-run logs, stop reasons, descriptions showing the configured values, and "~N left" estimates for the backfills.
- Incremental volume sync: an hourly job re-reads volumes ComicVine changed since its own watermark, like the issue sync. A manual *Volume sync rewind* job moves that watermark back N days (default 730) for a one-off re-read.
- `BACKFILL_MIN_YEAR` floor for proactive backfills (default 2010).
- Cover hashes in `comic_covers` for ComicManager's cover matching.
- `comicvine.db` and `latest.mcl` exports for ComicManager; MCL gap backfill.
- Queue for cache misses that hit a rate limit; persisted rate state; per-endpoint budgets.
- Watermark-based incremental issue sync; nightly database backups; hourly WAL checkpoint.
- Issue detail fetch and backfill for rows stored from list responses.
- Docker image published to GitHub Container Registry.

Fixed:

- Volume rows from list responses (`/volumes/`, search, the sync) no longer wipe stored character, people, concept and object lists.

## 0.1.0

Initial release: ComicVine-compatible proxy with a SQLite cache, FTS search, search cache, image cache, rate limiting and a stats dashboard.
