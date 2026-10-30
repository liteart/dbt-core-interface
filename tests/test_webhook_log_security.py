"""Webhook credentials must never be included in application logs."""

import logging
from unittest.mock import Mock

import pytest
import requests

from dbt_core_interface.quality import QualityAlert, WebhookAlertChannel


@pytest.mark.parametrize("fails", [False, True])
def test_webhook_logs_do_not_expose_url_or_exception_details(caplog, fails: bool) -> None:
    """Both successful and failed requests leave credentials out of logs."""
    secret = "private-webhook-token"  # noqa: S105 - synthetic test credential
    url = f"https://example.org/hooks/{secret}?key={secret}"
    channel = WebhookAlertChannel(url)
    response = Mock()
    if fails:
        response.raise_for_status.side_effect = requests.HTTPError(f"request to {url} failed")
    channel._session = Mock()
    channel._session.post.return_value = response

    with caplog.at_level(logging.DEBUG, logger="dbt_core_interface.quality"):
        assert channel.send(QualityAlert()) is not fails

    assert secret not in caplog.text
    assert url not in caplog.text
    assert "HTTPError" in caplog.text if fails else "sent successfully" in caplog.text
