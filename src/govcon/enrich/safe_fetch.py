"""Fetch an untrusted URL safely.

Attachment URLs come from a government feed, not from us, so every request:

- uses HTTPS (plain HTTP only when explicitly allowed);
- resolves the host and refuses loopback, private, link-local (including the
  cloud metadata address), reserved, multicast, and unspecified addresses;
- follows redirects manually and re-checks every hop;
- streams the body and stops at a byte cap instead of loading it first.

Residual risk: the HTTP client resolves the host again when it connects, so a
DNS answer that changes between the check and the connection (rebinding) is
not prevented here. Network egress policy is the defence for that.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[[str], list[str]]

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """The URL could not be fetched (HTTP error, transport error, or limit)."""


class FetchBlocked(FetchError):
    """The URL or a redirect target is not allowed (scheme or address)."""


class FetchTooLarge(FetchError):
    """The response exceeded the byte cap."""


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    status_code: int
    content: bytes
    content_type: str | None
    content_disposition: str | None


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _address_allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: str, *, resolver: Resolver, allow_http: bool = False) -> None:
    """Raise ``FetchBlocked`` unless ``url`` is an allowed public HTTP(S) target."""
    parts = urlsplit(url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme.lower() not in allowed_schemes:
        raise FetchBlocked(f"scheme {parts.scheme or '(none)'!r} is not allowed")
    if parts.username or parts.password:
        raise FetchBlocked("URLs with embedded credentials are not allowed")
    host = parts.hostname
    if not host:
        raise FetchBlocked("URL has no host")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = resolver(host)
        except OSError as exc:
            raise FetchError(f"could not resolve {host}: {exc}") from exc
    if not addresses:
        raise FetchError(f"could not resolve {host}")
    blocked = [a for a in addresses if not _address_allowed(a)]
    if blocked:
        raise FetchBlocked(f"{host} resolves to a non-public address")


def safe_fetch(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    resolver: Resolver | None = None,
    allow_http: bool = False,
    params: dict[str, str] | None = None,
    max_redirects: int = 5,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> FetchResult:
    """GET ``url`` with address checks on every hop and a streamed byte cap.

    ``params`` (for example an API key) are sent only to the first URL, never
    to a redirect target on another host.
    """
    resolver = resolver or system_resolver
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _fetch_once(
                client,
                url,
                max_bytes=max_bytes,
                resolver=resolver,
                allow_http=allow_http,
                params=params,
                max_redirects=max_redirects,
            )
        except _Retryable as exc:
            last_error = exc
        except httpx.TransportError as exc:
            last_error = FetchError(f"transport error: {type(exc).__name__}")
        if attempt + 1 < attempts:
            time.sleep(backoff_seconds * (2**attempt))
    if isinstance(last_error, _Retryable):
        raise FetchError(str(last_error))
    raise last_error or FetchError("fetch failed")


class _Retryable(FetchError):
    pass


def _fetch_once(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    resolver: Resolver,
    allow_http: bool,
    params: dict[str, str] | None,
    max_redirects: int,
) -> FetchResult:
    current = url
    first_host = urlsplit(url).hostname
    for _hop in range(max_redirects + 1):
        check_url(current, resolver=resolver, allow_http=allow_http)
        send_params = params if params and urlsplit(current).hostname == first_host else None
        request = client.build_request("GET", current, params=send_params)
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"HTTP {response.status_code} redirect without a location")
                current = urljoin(str(response.url), location)
                continue
            if response.status_code in _RETRYABLE:
                raise _Retryable(f"HTTP {response.status_code}")
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FetchTooLarge(f"declared size {int(declared)} bytes exceeds the {max_bytes}-byte limit")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FetchTooLarge(f"response exceeds the {max_bytes}-byte limit")
                chunks.append(chunk)
            return FetchResult(
                final_url=current,
                status_code=response.status_code,
                content=b"".join(chunks),
                content_type=response.headers.get("content-type"),
                content_disposition=response.headers.get("content-disposition"),
            )
        finally:
            response.close()
    raise FetchBlocked(f"more than {max_redirects} redirects")
