"""
Request body-size limit (OWASP A04 — unrestricted resource consumption).

nginx caps the body too (client_max_body_size), but the app must not depend on a
correctly-configured proxy being in front. A JSON request to this service is
always small; the one exception is the audio upload on /api/query/transcribe,
which keeps its own 10 MB ceiling in the router.

Plain ASGI middleware (not BaseHTTPMiddleware) so it can reliably wrap the
`receive` channel. Enforced two ways:
  1. Content-Length header over the limit  -> 413 immediately, body never read.
  2. No / understated Content-Length       -> bytes are counted as they arrive
     and the stream is cut off at the limit with a 413.
"""
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

_EXEMPT_PREFIXES = ("/api/query/transcribe",)


async def _send_413(send: Send) -> None:
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": b'{"detail":"request body too large"}'})


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        limit = settings.MAX_REQUEST_BYTES

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > limit:
                    await _send_413(send)
                    return
                break

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        # If we tripped the limit mid-stream we still need to answer with a 413
        # rather than letting a truncated body reach the app.
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        finally:
            if exceeded and not started:
                await _send_413(send)
