"""Tests for API key redaction in log output."""

from __future__ import annotations

import logging
from collections.abc import Generator

import httpx
import pytest

from cvproxy.main import install_api_key_redaction

_KEY = "0123456789abcdef0123456789abcdef01234567"


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def captured() -> Generator[tuple[logging.Logger, _ListHandler]]:
    original_factory = logging.getLogRecordFactory()
    install_api_key_redaction()
    logger = logging.getLogger("test.redaction")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, handler
    logger.removeHandler(handler)
    logging.setLogRecordFactory(original_factory)


def test_redacts_key_in_httpx_style_url_arg(captured: tuple[logging.Logger, _ListHandler]) -> None:
    logger, handler = captured
    url = httpx.URL(f"https://comicvine.gamespot.com/api/issues/?api_key={_KEY}&format=json")
    logger.info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 420, "")

    assert _KEY not in handler.lines[0]
    assert "api_key=<redacted>&format=json" in handler.lines[0]
    assert "420" in handler.lines[0]


def test_redaction_keeps_uvicorn_access_args_shape(
    captured: tuple[logging.Logger, _ListHandler],
) -> None:
    """uvicorn's AccessFormatter unpacks record.args as a 5-tuple."""
    logger, handler = captured
    seen: list[tuple] = []

    class _Unpacker(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            client_addr, method, full_path, http_version, status_code = record.args  # type: ignore[misc]
            seen.append((client_addr, method, full_path, http_version, status_code))
            return True

    handler.addFilter(_Unpacker())
    logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.1:5000", "GET", f"/api/issues/?api_key={_KEY}&filter=volume:1", "1.1", 200,
    )

    assert seen[0][2] == "/api/issues/?api_key=<redacted>&filter=volume:1"
    assert seen[0][4] == 200
    assert _KEY not in handler.lines[0]


def test_redacts_key_in_message_text(captured: tuple[logging.Logger, _ListHandler]) -> None:
    logger, handler = captured
    logger.warning(f"failed url https://x/api/?api_key={_KEY}")

    assert _KEY not in handler.lines[0]


def test_redacts_key_in_exception_traceback(
    captured: tuple[logging.Logger, _ListHandler],
) -> None:
    logger, handler = captured
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        raise RuntimeError(f"Client error for url 'https://x/api/?api_key={_KEY}&format=json'")
    except RuntimeError:
        logger.exception("upstream failed")

    assert _KEY not in handler.lines[0]
    assert "api_key=<redacted>" in handler.lines[0]
