"""One version number, reported by the app and sent upstream."""

from __future__ import annotations

from cvproxy import __version__
from cvproxy.app import create_app
from cvproxy.cv_client import CVClient


def test_app_and_upstream_requests_report_the_package_version() -> None:
    assert __version__ == "2.0.1"
    assert create_app().version == __version__
    client = CVClient(api_key="k")
    assert client._client.headers["User-Agent"] == f"CVProxy/{__version__}"
