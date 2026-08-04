"""Regression: the rate limiter must not trust a client-supplied header.

Taking the left-most X-Forwarded-For entry let any client invent a new bucket
per request simply by varying the header, which silently disabled rate
limiting for exactly the traffic it was meant to stop.

Also guards the calling convention slowapi imposes on key functions, which is
expressed only through a parameter name and so is invisible to mypy and ruff.
"""

from inspect import signature

import pytest
from starlette.requests import Request

from app.config import get_settings
from app.core.ratelimit import META_WEBHOOK_BUCKET, client_key, webhook_key

PEER = "198.51.100.7"
REAL_CLIENT = "203.0.113.9"


def _request(forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/webhook",
        "raw_path": b"/webhook",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (PEER, 51234),
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_falls_back_to_the_socket_peer_without_a_header() -> None:
    assert client_key(_request()) == PEER


def test_uses_the_entry_written_by_our_own_proxy() -> None:
    """nginx appends the peer it saw, so the right-most entry is the client."""
    assert get_settings().trusted_proxy_hops == 1
    key = client_key(_request(f"1.2.3.4, {REAL_CLIENT}"))
    assert key == REAL_CLIENT


def test_a_forged_prefix_cannot_change_the_bucket() -> None:
    """The whole point: spoofing the header must not yield a fresh quota."""
    first = client_key(_request(f"10.0.0.1, {REAL_CLIENT}"))
    second = client_key(_request(f"172.16.0.1, 8.8.8.8, {REAL_CLIENT}"))
    assert first == second == REAL_CLIENT


def test_short_chain_falls_back_to_the_peer() -> None:
    """Fewer entries than trusted hops means the header is not trustworthy."""
    assert client_key(_request("   ")) == PEER


def test_webhook_key_is_one_fixed_bucket() -> None:
    """Meta's address tells us nothing, so every delivery shares one bucket."""
    assert webhook_key(_request()) == META_WEBHOOK_BUCKET
    assert webhook_key(_request(f"1.2.3.4, {REAL_CLIENT}")) == META_WEBHOOK_BUCKET


@pytest.mark.parametrize("key_func", [client_key, webhook_key])
def test_key_functions_use_the_parameter_name_slowapi_requires(key_func) -> None:
    """Regression: every POST /webhook returned 500 over a parameter name.

    slowapi decides whether to pass the request by looking for a parameter
    literally named "request":

        if "request" in signature(lim.key_func).parameters.keys():
            limit_key = lim.key_func(request)
        else:
            limit_key = lim.key_func()

    webhook_key ignores its argument, so it had been named _request in the
    usual "unused" style. That sent slowapi down the zero-argument branch and
    raised TypeError inside the limiter on every webhook delivery -- Meta
    retried, the retries failed too, and no customer message got through.

    Asserted on the signature rather than by driving a request, because
    conftest.py disables rate limiting and slowapi then never calls these at
    all -- which is precisely how the bug reached production.
    """
    assert "request" in signature(key_func).parameters
