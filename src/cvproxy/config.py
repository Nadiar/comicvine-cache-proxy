"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """CVProxy configuration.

    All values can be set via environment variables (case-insensitive).
    """

    # ComicVine API
    cv_api_key: str
    cv_api_base_url: str = "https://comicvine.gamespot.com/api"
    rate_limit_per_minute: int = 100

    # Database
    db_path: Path = Path("/data/localcv.db")

    # Image cache
    image_cache_dir: Path = Path("/data/images")
    image_cache_ttl_days: int = 14

    # Stats
    stats_db_path: Path = Path("/data/stats.db")

    # Background sync
    sync_enabled: bool = True
    sync_cron_hour: int = 3  # UTC hour for daily sync

    # Cache eviction (all disabled by default — set > 0 to enable)
    evict_older_than_years: int = 0    # age cutoff; entities older than this become eviction-eligible
    evict_unaccessed_days: int = 180   # days since last access before an eligible entity is evicted
    response_cache_ttl_days: int = 30  # TTL for cv_response_cache (generic resources); 0 = keep forever
    search_cache_ttl_days: int = 60    # TTL for search result caches; 0 = keep forever
    search_max_pages: int = 5          # max upstream pages to fetch per search query (100 results/page)

    # Ingest cutoff — skip caching content older than this publication year (0 = disabled).
    # Issues: checked against store_date, then cover_date.
    # Volumes: checked against last_issue.cover_date (skipped when that field is absent).
    cache_cutoff_year: int = 0

    # ComicVine enforces a hard limit of 200 requests/hour per endpoint path.
    # Default 180 leaves a 10% safety buffer for organic proxy traffic.
    rate_limit_per_hour_per_endpoint: int = 180

    # Server
    host: str = "0.0.0.0"
    port: int = 8585

    model_config = {"env_prefix": "", "case_sensitive": False}


# Singleton — created once at startup, importable everywhere.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings instance (lazy-init)."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def override_settings(settings: Settings) -> None:
    """Replace the global settings (used in testing)."""
    global _settings
    _settings = settings
