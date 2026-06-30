"""CLI entry point for CVProxy."""

from __future__ import annotations

import logging

import uvicorn

from cvproxy.app import create_app
from cvproxy.config import get_settings


def main() -> None:
    """Run the CVProxy server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
