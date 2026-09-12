"""Network primitives copied/adapted from nanobot's web-tool security path.

Keeps ClearAct self-contained while providing SSRF checks, DNS pinning and safe redirects.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import suppress
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

MAX_REDIRECTS = 5
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class UnsafeURLRequestError(httpx.RequestError):
    """Request rejected because its target is internal or unsafe."""


def _normalise(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def _loopback_allowed(hostname: str, addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    if not addresses or not all(_normalise(address).is_loopback for address in addresses):
        return False
    if hostname.rstrip(".").lower() == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False


def resolve_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str, tuple[str, ...]]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'", ()
    if not parsed.netloc or not parsed.hostname:
        return False, "Missing domain", ()
    try:
        infos = socket.getaddrinfo(parsed.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {parsed.hostname}", ()
    addresses = []
    for info in infos:
        with suppress(ValueError):
            addresses.append(ipaddress.ip_address(info[4][0]))
    if not addresses:
        return False, f"Cannot resolve hostname: {parsed.hostname}", ()
    if allow_loopback and _loopback_allowed(parsed.hostname, addresses):
        return True, "", tuple(dict.fromkeys(str(_normalise(address)) for address in addresses))
    for address in addresses:
        if any(_normalise(address) in network for network in _BLOCKED_NETWORKS):
            return False, f"Blocked: {parsed.hostname} resolves to private/internal address {address}", ()
    return True, "", tuple(dict.fromkeys(str(_normalise(address)) for address in addresses))


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate the result, and connect to that exact address."""

    def __init__(self, *, allow_loopback: bool = False, backend=None):
        self.allow_loopback = allow_loopback
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        hostname = host.decode("ascii") if isinstance(host, bytes) else str(host)
        ok, error, addresses = resolve_url_target(
            f"http://[{hostname}]" if ":" in hostname and not hostname.startswith("[") else f"http://{hostname}",
            allow_loopback=self.allow_loopback,
        )
        if not ok:
            raise httpcore.ConnectError(error)
        last_error = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"Cannot connect to hostname: {hostname}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.UnsupportedProtocol("Unix sockets are not supported by the safe web transport")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class PinnedDNSAsyncTransport(httpx.AsyncHTTPTransport):
    """Validate requests and pin TCP connections without replacing process-wide DNS."""

    def __init__(self, *, allow_loopback: bool = False):
        super().__init__(trust_env=False)
        self.allow_loopback = allow_loopback
        # HTTPX does not expose the network backend in its public constructor.
        # The pool is new and has no connections yet, so replacing it here makes
        # every connection use the validating backend while preserving Host/SNI.
        self._pool._network_backend = PinnedNetworkBackend(allow_loopback=allow_loopback)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        ok, error, _ = resolve_url_target(url, allow_loopback=self.allow_loopback)
        if not ok:
            raise UnsafeURLRequestError(error, request=request)
        return await super().handle_async_request(request)


def client_kwargs(*, timeout: float, allow_loopback: bool) -> dict:
    """Build a direct client whose DNS validation cannot be bypassed by environment proxies."""
    return {
        "timeout": timeout,
        "trust_env": False,
        "transport": PinnedDNSAsyncTransport(allow_loopback=allow_loopback),
    }


async def get_with_safe_redirects(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], *, allow_loopback: bool = False
):
    """Validate every redirect before requesting it, as nanobot does."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response = await client.get(current, headers=headers, follow_redirects=False)
        if not 300 <= response.status_code < 400 or not response.headers.get("location"):
            return response
        next_url = urljoin(str(response.url), response.headers["location"])
        ok, error, _ = resolve_url_target(next_url, allow_loopback=allow_loopback)
        if not ok:
            await response.aclose()
            raise UnsafeURLRequestError(f"Redirect blocked: {error}", request=response.request)
        await response.aclose()
        current = next_url
    raise UnsafeURLRequestError(f"Too many redirects: exceeded limit of {MAX_REDIRECTS}")
