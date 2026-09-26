"""Shared HTTP client. Ingestion modules use this instead of their own stacks."""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from govcon.config import Settings, get_settings

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableStatus(Exception):
    """Internal signal that a response status should be retried."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"retryable HTTP {response.status_code}")


def build_client(settings: Settings | None = None, **overrides: object) -> httpx.Client:
    settings = settings or get_settings()
    headers = {"User-Agent": settings.http_user_agent}
    extra_headers = overrides.pop("headers", None)
    if isinstance(extra_headers, dict):
        headers.update(extra_headers)
    timeout = overrides.pop("timeout", 30.0)
    return httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, **overrides)


def request_with_retry(client: httpx.Client, method: str, url: str, **kwargs: object) -> httpx.Response:
    """Retry transport failures and 429/5xx responses with exponential backoff."""

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.05, min=0.05, max=0.5),
        reraise=True,
    )
    def _send() -> httpx.Response:
        response = client.request(method, url, **kwargs)
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableStatus(response)
        return response

    try:
        return _send()
    except RetryableStatus as exc:
        return exc.response
