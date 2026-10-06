"""Existing route tests submit the CSRF token a browser form would carry."""

import hashlib
import hmac
from html.parser import HTMLParser

from fastapi.testclient import TestClient


class CsrfTestClient(TestClient):
    def post(self, url, **kwargs):
        cookies = kwargs.get("cookies") or {}
        if not self.cookies.get("govcon_csrf"):
            self.get("/login", cookies=cookies, follow_redirects=False)
        nonce = cookies.get("govcon_csrf", self.cookies.get("govcon_csrf"))
        session = cookies.get("govcon_session", self.cookies.get("govcon_session", ""))
        message = f"{len(session)}:{session}:{nonce}".encode()
        token = hmac.new(self.app.state.csrf_secret, message, hashlib.sha256).hexdigest()
        kwargs["headers"] = {"X-CSRF-Token": token, **(kwargs.get("headers") or {})}
        return super().post(url, **kwargs)


class _NextPage(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.url = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("rel") == "next":
            self.url = attrs.get("href")


def page_containing(client, url, marker, **kwargs):
    """Follow the rendered pager, checking the same UI a user can navigate."""
    visited = set()
    for _ in range(1000):
        assert url not in visited, "pagination loop"
        visited.add(url)
        response = client.get(url, **kwargs)
        assert response.status_code == 200
        if marker in response.text:
            return response
        url = _NextPage(response.text).url
        assert url, f"Fixture {marker!r} is missing from the paginated view"
    raise AssertionError("pagination did not finish within 1000 pages")
