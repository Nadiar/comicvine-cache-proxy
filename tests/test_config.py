"""Tests for application settings."""

from cvproxy.config import Settings


def test_eviction_settings_have_correct_defaults() -> None:
    s = Settings(cv_api_key="x")
    assert s.evict_older_than_years == 0
    assert s.evict_unaccessed_days == 180
    assert s.response_cache_ttl_days == 30


def test_settings_works_without_eviction_fields() -> None:
    """Settings must work when eviction fields are omitted (backward compat)."""
    s = Settings(cv_api_key="testkey")
    assert s.cv_api_key == "testkey"
    assert s.evict_older_than_years == 0
