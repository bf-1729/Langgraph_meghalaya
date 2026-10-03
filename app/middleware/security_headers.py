"""
Security response headers + request id (OWASP A05 / A09).

Adds the standard hardening headers to every response:
  - X-Content-Type-Options: nosniff        (stop MIME sniffing)
  - X-Frame-Options: DENY                  (legacy clickjacking guard)
  - Content-Security-Policy                (settings.CSP_ENFORCE — frame-ancestors,
                                             base-uri, object-src, form-action are
                                             the parts that actually bite here)
  - Referrer-Policy: no-referrer
  - Permissions-Policy                     (deny camera / geolocation; mic = self only)
  - Cross-Origin-Opener-Policy / -Resource-Policy: same-origin
  - Strict-Transport-Security              (when settings.HSTS_ENABLED)
  - Cache-Control: no-store on /api/*      (keep answers/audit data out of caches)

Also mints/propagates an X-Request-ID so a 500 the caller sees can be tied to
the stack trace in the logs without leaking the trace itself.

Toggle the whole thing with settings.SECURITY_HEADERS_ENABLED.
"""
import uuid

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import settings
from app.context_budget import request_id_var

# microphone=(self) so the "Speak" voice-query feature on our own pages can call
# getUserMedia; still denied for cross-origin frames. camera/geolocation stay off.
_PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=(), interest-cohort=()"


def apply_security_headers(headers: MutableHeaders, path: str) -> None:
    """Set the hardening headers on `headers` in place. Shared by the middleware
    and by the global exception handler (an unhandled 500 is rendered outside the
    middleware stack, so it would otherwise miss them)."""
    if not settings.SECURITY_HEADERS_ENABLED:
        return
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if settings.CSP_ENFORCE:
        headers.setdefault("Content-Security-Policy", settings.CSP_ENFORCE)
    if settings.CSP_REPORT_ONLY:
        headers.setdefault("Content-Security-Policy-Report-Only", settings.CSP_REPORT_ONLY)
    if settings.HSTS_ENABLED:
        headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={settings.HSTS_MAX_AGE}; includeSubDomains",
        )
    if path.startswith("/api/"):
        headers["Cache-Control"] = "no-store"
    if "Server" in headers:
        del headers["Server"]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id", "").strip()[:64] or uuid.uuid4().hex
        request.state.request_id = rid
        # The same id on every prompt_context log line of this request
        # (app/context_budget.py). call_next runs the app in a task that copies
        # this context, so the endpoint sees it.
        request_id_var.set(rid)

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        apply_security_headers(response.headers, request.url.path)
        return response
