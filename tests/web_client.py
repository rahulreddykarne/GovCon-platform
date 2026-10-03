"""Existing route tests submit the CSRF token a browser form would carry."""

import hashlib
import hmac

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
