"""The SSRF guard for the dashboard's test crawl.

Anyone who can reach the dashboard's port can start a test crawl, and a test
crawl fetches whatever address it is given. Without this check that is a
free HTTP client into the customer's own network: the database container,
a cloud metadata endpoint (``169.254.169.254``), the reverse proxy's admin
page. So before the first request - and again on every redirect hop, because
a public host may answer with a redirect to a private one - the address is
resolved and every resulting IP must be public.

The stand-in site used by the tests lives on loopback; those tests run with
``DASHBOARD_ALLOW_PRIVATE_HOSTS=true``, which is the only way past this
check. It is the caller's job to pin the returned IPs into the request it
makes, so that a name cannot resolve to something else between the check
and the fetch (DNS rebinding).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

Resolver = "callable[[str], list[str]]"


class NetGuardError(ValueError):
    """Raised with a sentence the UI can show as it is."""


def is_public_ip(ip: str) -> bool:
    """Is this address one the crawl may talk to?

    >>> is_public_ip("93.184.216.34"), is_public_ip("2606:2800:220:1:248:1893:25c8:1946")
    (True, True)
    >>> [is_public_ip(a) for a in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1")]
    [False, False, False, False]
    >>> [is_public_ip(a) for a in ("169.254.169.254", "224.0.0.1", "0.0.0.0", "255.255.255.255", "100.64.0.1")]
    [False, False, False, False, False]
    >>> [is_public_ip(a) for a in ("::1", "fe80::1", "fd00::1", "ff02::1", "::")]
    [False, False, False, False, False]

    An IPv4 address hidden in an IPv6 mapping is judged as the IPv4 address:

    >>> is_public_ip("::ffff:127.0.0.1")
    False
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr.sixtofour is not None:
        addr = addr.sixtofour
    # `is_global` is the IANA special-purpose registry in one bit: it also
    # covers the shared address space 100.64/10 (carrier NAT, often what a
    # customer's own network uses) that `is_private` does not.
    return addr.is_global and not (
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_multicast or addr.is_unspecified or addr.is_reserved)


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def check_public(url: str, allow_private: bool = False, resolve=None) -> list[str]:
    """Resolve ``url``'s host and return its IPs, or raise `NetGuardError`.

    ``resolve`` is for tests: a function from host name to a list of IPs,
    replacing the system resolver.

    >>> check_public("http://93.184.216.34/")
    ['93.184.216.34']
    >>> check_public("http://127.0.0.1:8001/", allow_private=True)
    ['127.0.0.1']
    >>> check_public("ftp://93.184.216.34/")
    Traceback (most recent call last):
    ...
    crawlkit.netguard.NetGuardError: Only http and https addresses can be tested, not ftp.
    >>> check_public("http://[::1]/")
    Traceback (most recent call last):
    ...
    crawlkit.netguard.NetGuardError: This address points at a private network (::1) and cannot be tested.
    """
    parts = urlsplit(url.strip() if url else "")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        shown = scheme or "an address without a scheme"
        raise NetGuardError(f"Only http and https addresses can be tested, not {shown}.")
    host = parts.hostname or ""
    if not host:
        raise NetGuardError("This address has no host name.")
    try:
        ips = (resolve or _resolve)(host)
    except (socket.gaierror, OSError) as exc:
        raise NetGuardError(f"The host name {host} could not be resolved ({exc}).") from exc
    if not ips:
        raise NetGuardError(f"The host name {host} could not be resolved.")
    if allow_private:
        return list(ips)
    private = [ip for ip in ips if not is_public_ip(ip)]
    if private:
        raise NetGuardError(
            f"This address points at a private network ({', '.join(private)}) "
            "and cannot be tested.")
    return list(ips)


__all__ = ["NetGuardError", "check_public", "is_public_ip"]
