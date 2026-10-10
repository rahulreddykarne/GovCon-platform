"""Shared HTTP client. Ingestion modules use this instead of their own stacks.

Connects try IPv4 before IPv6 and always have a connect timeout. A black-holed
IPv6 route must fail and be retried instead of sitting in SYN_SENT.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from typing import Any

import httpcore
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

from govcon.config import Settings, get_settings

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Short enough that a dead IPv6 route cannot hold the worker for minutes.
CONNECT_TIMEOUT_SECONDS = 10.0
_IPV6_ATTEMPTS = 2


class RetryableStatus(Exception):
    """Internal signal that a response status should be retried."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"retryable HTTP {response.status_code}")


def split_timeout(read_seconds: float) -> httpx.Timeout:
    """Connect is capped. ``read_seconds`` is the body timeout and is not used for TCP setup."""
    read = float(read_seconds)
    if read <= 0:
        raise ValueError("timeout must be positive")
    connect = min(CONNECT_TIMEOUT_SECONDS, read)
    return httpx.Timeout(connect=connect, read=read, write=min(30.0, read), pool=connect)


def connect_targets(host: str, port: int) -> list[str]:
    """IPv4 addresses first, then at most two IPv6 addresses. No network of our own."""
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    v4: list[str] = []
    v6: list[str] = []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if family == socket.AF_INET and address not in v4:
            v4.append(address)
        elif family == socket.AF_INET6 and address not in v6:
            v6.append(address)
    return v4 + v6[:_IPV6_ATTEMPTS]


class IPv4FirstBackend(httpcore.SyncBackend):
    """Try IPv4, then a couple of IPv6 addresses, each under the connect timeout."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        limit = CONNECT_TIMEOUT_SECONDS if timeout is None else min(float(timeout), CONNECT_TIMEOUT_SECONDS)
        try:
            targets = connect_targets(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(exc) from exc
        if not targets:
            targets = [host]
        last: BaseException | None = None
        for address in targets:
            try:
                return super().connect_tcp(
                    address, port, timeout=limit, local_address=local_address, socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001  try the next address
                last = exc
        if last is not None:
            raise last
        raise httpcore.ConnectError(f"no route to {host}")


def ipv4_first_transport() -> httpx.HTTPTransport:
    """Default transport whose pool connects IPv4-first. Tests pass their own transport."""
    transport = httpx.HTTPTransport()
    pool = getattr(transport, "_pool", None)
    if pool is not None and hasattr(pool, "_network_backend"):
        pool._network_backend = IPv4FirstBackend()
    return transport


def build_client(
    settings: Settings | None = None,
    *,
    timeout: float | httpx.Timeout = 30.0,
    headers: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    settings = settings or get_settings()
    merged = {"User-Agent": settings.http_user_agent}
    if headers:
        merged.update(headers)
    if isinstance(timeout, httpx.Timeout):
        read = timeout.read if timeout.read is not None else 30.0
        timeout = split_timeout(read)
    else:
        timeout = split_timeout(float(timeout))
    if transport is None:
        transport = ipv4_first_transport()
    return httpx.Client(
        headers=merged, timeout=timeout, follow_redirects=True, transport=transport,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    wait: wait_base | None = None,
    params: Mapping[str, str] | None = None,
    json: Mapping[str, Any] | None = None,
    data: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Retry transport failures and 429/5xx responses with exponential backoff.

    The default wait stays short so tests remain fast. Callers that talk to a
    quota-limited API can pass a longer ``wait`` and a higher ``attempts``.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    policy = wait or wait_exponential(multiplier=0.05, min=0.05, max=0.5)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
        stop=stop_after_attempt(attempts),
        wait=policy,
        reraise=True,
    )
    def _send() -> httpx.Response:
        response = client.request(method, url, params=params, json=json, data=data)
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableStatus(response)
        return response

    try:
        return _send()
    except RetryableStatus as exc:
        return exc.response
