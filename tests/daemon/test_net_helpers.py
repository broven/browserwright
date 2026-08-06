"""`daemon/_net.py` — the loopback predicate and the URL redactor.

Both rules used to live inline at their single call site. They moved here
because each is now asked from several places (proxy bypass, `trust_env`, the
DevToolsActivePort fallback; daemon logs, `daemon ps --json`, `session list
--json`, resolver errors), and a rule answered twice is a rule that drifts.
"""
from __future__ import annotations

import pytest

from browserwright.daemon._net import is_loopback_host, redact_url


# ---- is_loopback_host ------------------------------------------------------


@pytest.mark.parametrize("value", [
    "127.0.0.1",
    "localhost",
    "LOCALHOST",
    "localhost.",          # legal FQDN spelling of the same name
    "::1",
    "[::1]",               # bare host handed in with brackets
    "ws://127.0.0.1:9222/devtools/browser/abc",
    "http://localhost:9222",
    "ws://[::1]:9222/devtools/browser/abc",
])
def test_loopback_hosts(value):
    assert is_loopback_host(value) is True


@pytest.mark.parametrize("value", [
    "127.0.0.2",
    "127.1.2.3",
    "http://127.0.0.53:9222",
])
def test_whole_127_range_is_loopback(value):
    """The literal tuple this replaced only listed `127.0.0.1`.

    Chrome will bind to any address in `127.0.0.0/8`, and treating those as
    remote applied the user's proxy to a browser on their own machine.
    """
    assert is_loopback_host(value) is True


@pytest.mark.parametrize("value", [
    "cloud.example.com",
    "192.168.1.10",
    "10.0.0.1",
    "::2",
    "wss://connect.browserbase.com/?apiKey=x",
    "ws://192.168.1.10:9222/devtools/browser/abc",
])
def test_remote_hosts(value):
    assert is_loopback_host(value) is False


@pytest.mark.parametrize("value", ["", "   ", "not a url", "://", None, 42, [], "[]"])
def test_unidentifiable_is_treated_as_remote(value):
    """Fail safe, not fail open.

    An unparseable host must not win the proxy bypass or the local-only
    DevToolsActivePort fallback — both are only correct for a browser we can
    positively identify as local.
    """
    assert is_loopback_host(value) is False


# ---- redact_url ------------------------------------------------------------


def test_redacts_userinfo_keeps_endpoint_identity():
    out = redact_url("wss://user:s3cr3t@cloud.example.com:443/cdp")
    assert "s3cr3t" not in out
    assert "user" not in out
    assert "<redacted>@cloud.example.com:443" in out
    assert out.endswith("/cdp")


def test_redacts_query_string():
    out = redact_url("ws://cloud.example.com/cdp?apiKey=deadbeef&x=1")
    assert "deadbeef" not in out
    assert out == "ws://cloud.example.com/cdp?<redacted>"


def test_keeps_token_free_urls_intact():
    url = "ws://127.0.0.1:9222/devtools/browser/2f1c-4b9a"
    assert redact_url(url) == url


@pytest.mark.parametrize("value", [None, 42, "", "not-a-url", ["ws://x/y"]])
def test_non_urls_pass_through_unchanged(value):
    """Safe to apply blindly to a field that may hold anything."""
    assert redact_url(value) == value


def test_unparseable_url_is_not_leaked_verbatim():
    # An invalid port makes urlsplit raise only when `.port` is touched; the
    # guarantee that matters is that we never fall through returning the raw
    # string, which is what would leak a token in a malformed endpoint.
    out = redact_url("ws://host:notaport/path?token=secret")
    assert "secret" not in str(out)
