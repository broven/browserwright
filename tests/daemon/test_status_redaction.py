"""`daemon ps --json` is meant to be pasted into a bug report, so the upstream
URL it reports must not carry a credential. BD_CDP_WS for a cloud or
anti-detect browser routinely does — in the userinfo or as a query token — and
that token is reusable.
"""
import pytest

from browserwright.daemon.server.status import _redact_ws_url


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:29990/devtools/browser/abc-123",
    "ws://127.0.0.1:19989/__extension_relay__",
    "ext://relay",
])
def test_local_urls_survive_intact(url):
    """Redaction must not cost diagnostic value where there is no secret."""
    assert _redact_ws_url(url) == url


def test_userinfo_is_removed():
    out = _redact_ws_url("wss://user:s3cr3t@cloud.example.com:443/cdp")
    assert "s3cr3t" not in out and "user" not in out
    # The endpoint identity is what the field is for, so it stays.
    assert "cloud.example.com:443" in out and out.endswith("/cdp")


def test_query_token_is_removed():
    out = _redact_ws_url("ws://cloud.example.com/cdp?apiKey=deadbeef&x=1")
    assert "deadbeef" not in out and "apiKey" not in out
    assert out.startswith("ws://cloud.example.com/cdp")


def test_non_urls_and_none_pass_through():
    assert _redact_ws_url(None) is None
    assert _redact_ws_url(42) == 42
