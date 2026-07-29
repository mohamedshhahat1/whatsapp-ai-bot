"""Regression: the rate limiter must not trust a client-supplied header.

Taking the left-most X-Forwarded-For entry let any client invent a new bucket
per request simply by varying the header, which silently disabled rate
limiting for exactly the traffic it was meant to stop.
"""

from starlette.requests import Request

from app.config import get_settings
from app.core.ratelimit import client_key

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
