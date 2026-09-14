"""Tests for the SSRF guard.

The resolver is replaced by a dictionary so that nothing here touches DNS;
the one exception resolves a literal IP, which the system resolver answers
without a network.

    python -m pytest crawlkit -q
"""

from __future__ import annotations

import socket

import pytest

from crawlkit.netguard import NetGuardError, check_public, is_public_ip

FAKE_DNS = {
    "public.example": ["93.184.216.34"],
    "public6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "dual.example": ["93.184.216.34", "10.0.0.7"],       # one public, one private
    "localhost": ["127.0.0.1", "::1"],
    "db.internal": ["10.1.2.3"],
    "office.internal": ["192.168.1.10"],
    "docker.internal": ["172.17.0.2"],
    "metadata.internal": ["169.254.169.254"],
    "ula.internal": ["fd00::1"],
    "linklocal6.internal": ["fe80::1"],
    "mapped.internal": ["::ffff:192.168.0.1"],
    "multicast.internal": ["224.0.0.1"],
    "nowhere.internal": [],
}


def fake_resolve(host: str) -> list[str]:
    if host not in FAKE_DNS:
        raise socket.gaierror(-2, "Name or service not known")
    return FAKE_DNS[host]


@pytest.mark.parametrize("ip, public", [
    ("93.184.216.34", True), ("8.8.8.8", True), ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("127.0.0.1", False), ("127.255.255.254", False), ("10.1.2.3", False),
    ("172.16.0.1", False), ("172.31.255.255", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("224.0.0.1", False), ("0.0.0.0", False),
    ("255.255.255.255", False), ("100.64.0.1", False),
    ("::1", False), ("::", False), ("fe80::1", False), ("fd00::1", False), ("fc00::1", False),
    ("ff02::1", False), ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False),
    ("2002:7f00:1::1", False),                                    # 6to4 of 127.0.0.1
    ("not-an-ip", False),
])
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


@pytest.mark.parametrize("host", [
    "localhost", "db.internal", "office.internal", "docker.internal", "metadata.internal",
    "ula.internal", "linklocal6.internal", "mapped.internal", "multicast.internal", "dual.example",
])
def test_private_hosts_are_refused_with_a_sentence(host):
    with pytest.raises(NetGuardError) as info:
        check_public(f"https://{host}/list", resolve=fake_resolve)
    assert str(info.value).startswith("This address points at a private network (")
    assert str(info.value).endswith(") and cannot be tested.")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8001/", "http://[::1]:8001/x", "http://10.0.0.1/"])
def test_ip_literals_are_refused_without_dns(url):
    with pytest.raises(NetGuardError):
        check_public(url)


def test_public_hosts_pass_and_return_the_ips_to_pin():
    assert check_public("https://public.example/list?x=1", resolve=fake_resolve) == ["93.184.216.34"]
    assert check_public("http://public6.example/", resolve=fake_resolve) == ["2606:2800:220:1:248:1893:25c8:1946"]
    assert check_public("http://93.184.216.34/") == ["93.184.216.34"]


def test_allow_flag_lets_the_standin_site_through():
    assert check_public("http://localhost:8001/", allow_private=True, resolve=fake_resolve) == ["127.0.0.1", "::1"]
    assert check_public("http://127.0.0.1:8001/", allow_private=True) == ["127.0.0.1"]


@pytest.mark.parametrize("url, fragment", [
    ("ftp://public.example/x", "not ftp"),
    ("file:///etc/passwd", "not file"),
    ("public.example/x", "without a scheme"),
    ("", "without a scheme"),
    ("http:///x", "no host name"),
])
def test_only_http_and_https(url, fragment):
    with pytest.raises(NetGuardError) as info:
        check_public(url, resolve=fake_resolve)
    assert fragment in str(info.value)


def test_unresolvable_host_is_an_error_not_a_pass():
    with pytest.raises(NetGuardError) as info:
        check_public("https://missing.example/", resolve=fake_resolve)
    assert "could not be resolved" in str(info.value)
    with pytest.raises(NetGuardError):
        check_public("https://nowhere.internal/", resolve=fake_resolve)


def test_redirect_hop_is_checked_again():
    # A public host answering with a redirect into the private network: the
    # caller runs check_public() on every Location it follows, and the second
    # call is what stops it.
    assert check_public("https://public.example/start", resolve=fake_resolve)
    with pytest.raises(NetGuardError):
        check_public("http://metadata.internal/latest/meta-data/", resolve=fake_resolve)


def test_scheme_and_host_are_case_insensitive():
    assert check_public("HTTPS://Public.Example/", resolve=lambda h: FAKE_DNS[h.lower()])
