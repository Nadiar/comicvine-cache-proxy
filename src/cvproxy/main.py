"""CLI entry point for CVProxy."""

from __future__ import annotations

import logging
import re
from typing import Any

import uvicorn

from cvproxy.app import create_app
from cvproxy.config import get_settings
from cvproxy.jobs import JobTagFilter, install_run_log_capture

_API_KEY_RE = re.compile(r"(api_key=)[^&\s'\"]+")


def _redact(text: str) -> str:
    return _API_KEY_RE.sub(r"\1<redacted>", text)


def _redact_arg(arg: Any) -> Any:
    """Redact one %-format argument, leaving non-matching args (and their types) alone.

    uvicorn's AccessFormatter unpacks ``record.args`` positionally and formats
    the status code with ``%d``, so the tuple shape and int args must survive.
    """
    if isinstance(arg, (int, float)):
        return arg
    text = str(arg)
    return _redact(text) if "api_key=" in text else arg


def install_api_key_redaction() -> None:
    """Strip ``api_key`` values from every log record before it is formatted.

    Covers httpx's request lines (the upstream URL carries the real ComicVine
    key), uvicorn access lines (clients may send their key to us) and
    exception tracebacks whose messages embed a request URL.
    """
    base_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        if record.exc_info:
            # Formatter.format() reuses a pre-set exc_text instead of re-rendering
            record.exc_text = _redact(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)


def install_job_logging() -> None:
    """Tag log lines written inside a job with "[job_id#run]" and keep its log.

    APScheduler's own "Running job"/"executed successfully" lines are dropped:
    the job runner logs a start and an end line with status and summary, and
    the 2-minute pending fetch alone produced 1,400 of them a day.
    """
    tag = JobTagFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(tag)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    install_run_log_capture()


def main() -> None:
    """Run the CVProxy server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(jobtag)s%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    install_api_key_redaction()
    install_job_logging()

    settings = get_settings()
    app = create_app()

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
