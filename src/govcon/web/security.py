"""Session-bound CSRF tokens, exact origin checks, and bounded login attempts."""

from collections import OrderedDict
import hashlib
import hmac
import secrets
from threading import Lock
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

CSRF_COOKIE = "govcon_csrf"


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
    form = await request.form()
    values = form.getlist("csrf_token")
    header = request.headers.get("x-csrf-token")
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
