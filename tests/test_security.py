"""
OWASP-hardening checks — headers, body/field limits, endpoint gating, login
throttle, error hygiene, secret guard.

Runs without a DB or the model gateway: every assertion is about middleware /
validation / routing that fires before those are touched. Plain script (no
pytest in the venv) — `python tests/test_security.py`, exit code 0 = all pass.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Force a known config before anything imports app.config. Real env vars win
# over the repo .env file in pydantic-settings.
os.environ.update(
    ENV="prod",                       # docs disabled, secret guard at CRITICAL
    AUTH_ENABLED="true",
    METRICS_TOKEN="test-metrics-token",
    JWT_SECRET="short",               # deliberately weak -> insecure_secrets() flags it
    MAX_QUESTION_CHARS="2000",
    MAX_REQUEST_BYTES="262144",
    LOGIN_RL_MAX="6",
    RATELIMIT_BACKEND="memory",
    DATABASE_URL="postgresql://u:p@127.0.0.1:5/none",
)

from starlette.testclient import TestClient  # noqa: E402

from app.config import insecure_secrets  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


print("1. SECURITY HEADERS")
r = client.get("/health")
h = r.headers
check("X-Content-Type-Options: nosniff", h.get("x-content-type-options") == "nosniff")
check("X-Frame-Options: DENY", h.get("x-frame-options") == "DENY")
check("Content-Security-Policy present", "content-security-policy" in h)
check("CSP has frame-ancestors 'none'", "frame-ancestors 'none'" in h.get("content-security-policy", ""))
check("Referrer-Policy: no-referrer", h.get("referrer-policy") == "no-referrer")
check("Permissions-Policy present", "permissions-policy" in h)
check("Strict-Transport-Security present", "strict-transport-security" in h)
check("X-Request-ID present", bool(h.get("x-request-id")))
check("no Server header", "server" not in h)

print("2. DOCS DISABLED IN PROD")
check("/docs -> 404", client.get("/docs").status_code == 404)
check("/openapi.json -> 404", client.get("/openapi.json").status_code == 404)

print("3. BODY-SIZE LIMIT (A04)")
big = client.post("/api/query", content=b'{"question":"' + b"x" * 300_000 + b'"}',
                  headers={"content-type": "application/json"})
check("300 KB body -> 413", big.status_code == 413, f"got {big.status_code}")

print("4. FIELD-LENGTH LIMIT (A03)")
# Body validation only runs once the auth dependency passes — override it so
# these assertions exercise the Pydantic constraints, not the 401 gate.
from app import auth as _auth               # noqa: E402
from app.routers.query import _identity     # noqa: E402

app.dependency_overrides[_identity] = lambda: _auth.anonymous_scope()
try:
    long_q = client.post("/api/query", json={"question": "a" * 3000})
    check("3000-char question -> 422", long_q.status_code == 422, f"got {long_q.status_code}")
    empty_q = client.post("/api/query", json={"question": ""})
    check("empty question -> 422", empty_q.status_code == 422, f"got {empty_q.status_code}")
    bad_sid = client.post("/api/query", json={"question": "hi", "session_id": "a b/c"})
    check("malformed session_id -> 422", bad_sid.status_code == 422, f"got {bad_sid.status_code}")
finally:
    app.dependency_overrides.pop(_identity, None)

print("5. ENDPOINT GATING (A01)")
check("/metrics no token -> 401", client.get("/metrics").status_code == 401)
check("/metrics wrong token -> 401",
      client.get("/metrics", headers={"x-metrics-token": "nope"}).status_code == 401)
check("/metrics right token -> 200",
      client.get("/metrics", headers={"x-metrics-token": "test-metrics-token"}).status_code == 200)
check("POST /api/rag/reingest anon -> 401/403",
      client.post("/api/rag/reingest").status_code in (401, 403))
hj = client.get("/health").json()
check("/health anon body is status-only", set(hj) == {"status"}, f"got keys {set(hj)}")
hj_priv = client.get("/health", headers={"x-metrics-token": "test-metrics-token"}).json()
check("/health privileged body has components", "components" in hj_priv)
check("/health privileged body flags insecure_config", "insecure_config" in hj_priv)

print("6. LOGIN THROTTLE (A07)")
codes = [client.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code
         for _ in range(8)]
check("a 429 appears within 8 rapid logins", 429 in codes, f"codes={codes}")

print("7. ERROR HYGIENE (A05)")

def _boom():
    raise RuntimeError("secret internal detail: dsn=postgres://user:pw@host/db")

app.dependency_overrides[_identity] = _boom
try:
    er = client.post("/api/query", json={"question": "hello"})
finally:
    app.dependency_overrides.pop(_identity, None)
body = er.text
check("unhandled error -> 500", er.status_code == 500, f"got {er.status_code}")
check("error body is the generic shape",
      er.json().get("detail") == "internal error", body[:200])
check("no exception text leaked to client", "secret internal detail" not in body)
check("no 'Traceback' in error body", "Traceback" not in body)
check("error body carries request_id", bool(er.json().get("request_id")))
check("error response carries X-Request-ID header", bool(er.headers.get("x-request-id")))
check("security headers present on the 500", er.headers.get("x-content-type-options") == "nosniff")

print("8. SECRET GUARD")
check("insecure_secrets() flags weak JWT_SECRET",
      any("JWT_SECRET" in s for s in insecure_secrets()), str(insecure_secrets()))

print()
if _fails:
    print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
    sys.exit(1)
print("ALL SECURITY CHECKS PASSED")
