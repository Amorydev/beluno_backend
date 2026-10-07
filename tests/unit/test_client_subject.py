from __future__ import annotations

from starlette.requests import Request

from beluno.api.dependencies import client_subject


def subject(host: str | None) -> str:
    scope = {"type": "http", "client": (host, 1234) if host else None, "headers": []}
    return client_subject(Request(scope))


def test_ipv6_clients_share_a_limit_per_64_block() -> None:
    assert (
        subject("2001:db8:1:2:aaaa::1") == subject("2001:db8:1:2:ffff::9") == ("2001:db8:1:2::/64")
    )
    assert subject("2001:db8:1:3::1") != subject("2001:db8:1:2::1")


def test_ipv4_and_mapped_addresses_count_per_address() -> None:
    assert subject("203.0.113.7") == "203.0.113.7"
    assert subject("::ffff:203.0.113.7") == "203.0.113.7"
    assert subject("testclient") == "testclient"
    assert subject(None) == "unknown"
