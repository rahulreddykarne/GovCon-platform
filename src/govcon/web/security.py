"""Session-bound CSRF tokens, exact origin checks, and bounded login attempts."""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from tempfile import SpooledTemporaryFile
from threading import Lock
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE = "govcon_csrf"
PACKAGE_UPLOAD_LIMIT = 50_000_000
FORM_BODY_LIMIT = 1_000_000
SOURCING_UPLOAD_LIMIT = 25 * 1024 * 1024 + FORM_BODY_LIMIT


class PackageUploadLimitMiddleware:
    """Bound the actual multipart stream before it is parsed or spooled."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit = (PACKAGE_UPLOAD_LIMIT if path.endswith("/submission/package/assemble") else
                 SOURCING_UPLOAD_LIMIT if path == "/suppliers" or path.endswith("/quotes") else FORM_BODY_LIMIT)
        received = 0
        # Reject oversize streams before calling the inner app. Exceptions from
        # receive are wrapped by BaseHTTPMiddleware and cannot reliably become 413.
        with SpooledTemporaryFile(max_size=1_000_000) as body:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                received += len(chunk)
                if received > limit:
                    await PlainTextResponse("Upload exceeds the request size limit", status_code=413)(scope, receive, send)
                    return
                await run_in_threadpool(body.write, chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            remaining = received

            async def bounded_receive() -> Message:
                nonlocal remaining
                if remaining < 0:
                    return await receive()
                chunk = await run_in_threadpool(body.read, 65_536)
                remaining -= len(chunk)
                more = remaining > 0
                if not more:
                    remaining = -1
                return {"type": "http.request", "body": chunk, "more_body": more}

            await self.app(scope, bounded_receive, send)


def secure_cookies(request: Request) -> bool:
    settings = request.app.state.settings
    return bool(settings.web_secure_cookies or request.url.scheme == "https"
                or (settings.web_public_origin or "").lower().startswith("https://"))


def csrf_token(request: Request) -> str:
    nonce = request.state.csrf_nonce
    session = request.cookies.get("govcon_session", "")
    message = f"{len(session)}:{session}:{nonce}".encode()
    return hmac.new(request.app.state.csrf_secret, message, hashlib.sha256).hexdigest()


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        return (parsed.scheme.lower(), parsed.hostname.lower(), port)
    except ValueError:
        return None


class LoginThrottle:
    """Per-process, per-client attempt windows; memory stays bounded."""

    def __init__(self, *, capacity: int = 4096):
        self.capacity = capacity
        self.entries: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.lock = Lock()

    def allow(self, key: str, *, limit: int, window: int) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.entries and next(iter(self.entries.values()))[0] <= now - window:
                self.entries.popitem(last=False)
            start, count = self.entries.get(key, (now, 0))
            if key not in self.entries and len(self.entries) >= self.capacity:
                return False
            if count >= limit:
                return False
            self.entries[key] = (start, count + 1)
            return True


async def protect_mutation(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.url.path.endswith("/submission/package/assemble"):
        length = request.headers.get("content-length", "")
        if not length.isascii() or not length.isdigit():
            raise HTTPException(411, "Package uploads require a content length")
        if int(length) > PACKAGE_UPLOAD_LIMIT:
            raise HTTPException(413, "Package uploads are limited to 50 MB including form data")
    settings = request.app.state.settings
    target = _origin(settings.web_public_origin or str(request.base_url))
    origins = request.headers.getlist("origin")
    referers = request.headers.getlist("referer")
    if len(origins) > 1 or len(referers) > 1:
        raise HTTPException(403, "Invalid request origin")
    source = origins[0] if origins else (referers[0] if referers else None)
    if target is None or (source is not None and _origin(source) != target):
        raise HTTPException(403, "Invalid request origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site submission refused")
    if not request.cookies.get(CSRF_COOKIE):
        raise HTTPException(403, "Invalid CSRF token; reload the page")
    header = request.headers.get("x-csrf-token")
    if header is not None and (len(header) != 64 or not header.isascii()
                               or not hmac.compare_digest(header, csrf_token(request))):
        raise HTTPException(403, "Invalid CSRF token; reload the page")
    form = await request.form()
    values = form.getlist("csrf_token")
    submitted = header if header is not None else (values[0] if len(values) == 1 else "")
    if (len(values) > 1 or not request.cookies.get(CSRF_COOKIE) or not isinstance(submitted, str)
            or len(submitted) != 64 or not submitted.isascii()
            or not hmac.compare_digest(submitted, csrf_token(request))):
        raise HTTPException(403, "Invalid CSRF token; reload the page")
    if request.url.path == "/login":
        key = request.client.host if request.client else "unknown"
        if not request.app.state.login_throttle.allow(
            key, limit=settings.login_attempt_limit, window=settings.login_attempt_window_seconds
        ):
            raise HTTPException(429, "Too many login attempts; try again later",
                                headers={"Retry-After": str(settings.login_attempt_window_seconds)})


async def csrf_cookie_middleware(request: Request, call_next):
    nonce = request.cookies.get(CSRF_COOKIE, "")
    if len(nonce) != 64 or any(c not in "0123456789abcdef" for c in nonce):
        nonce = secrets.token_hex(32)
    request.state.csrf_nonce = nonce
    response = await call_next(request)
    if request.cookies.get(CSRF_COOKIE) != nonce:
        response.set_cookie(CSRF_COOKIE, nonce, httponly=True, secure=secure_cookies(request),
                            samesite="lax", max_age=request.app.state.settings.session_ttl_hours * 3600)
    return response
