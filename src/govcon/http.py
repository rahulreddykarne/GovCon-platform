"""Shared HTTP client. Ingestion modules use this instead of their own stacks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


class RetryableStatus(Exception):
    """Internal signal that a response status should be retried."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"retryable HTTP {response.status_code}")


def build_client(
    settings: Settings | None = None,
    *,
    timeout: float = 30.0,
    headers: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    settings = settings or get_settings()
    merged = {"User-Agent": settings.http_user_agent}
    if headers:
        merged.update(headers)
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
