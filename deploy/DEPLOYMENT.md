# Deployment

Target: two application VMs behind the ESDS vFirewall, each running nginx plus
the systemd unit. Capacity budget 20–40 concurrent, 100–200 DAU — see
[../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

The service is a single FastAPI app (`app.main:app`). It talks to Postgres
(`megh_db`) and the self-hosted Qwen gateway on `10.48.242.4`, and Qdrant on `115.124.102.167:6335`.

---

## 1. Prerequisites on the box

| | |
| --- | --- |
| Python | **3.11+** (verified on 3.11.9) |
| Reachable | `10.48.242.4:5432` (Postgres), `:443` (model gateway); `115.124.102.167:6335` (Qdrant, since 2026-10-03) |
| User | `ubuntu` (matches `User=` in the unit) |

## 2. Lay the code down

```bash
sudo mkdir -p /opt/meghalaya
sudo chown ubuntu:ubuntu /opt/meghalaya
# copy the repo (git clone / rsync / tarball) so it looks like:
#   /opt/meghalaya/app/  data/  web/  deploy/  docs/  requirements.txt
```

The tree must keep its shape: `app/` resolves `data/` and `web/` as siblings
(`Path(__file__).resolve().parents[1]`). Moving `app/` alone will break KB
ingest, the entity resolver, and the static UI routes.

## 3. Virtualenv

```bash
cd /opt/meghalaya
python3.11 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.lock     # reproducible
# or:  ./.venv/bin/pip install -r requirements.txt   (compatible updates)
```

`fastembed` pulls `onnxruntime` and downloads the `bge-small-en-v1.5` model on
first use (`EMBEDDING_PROVIDER=local`). Allow outbound HTTPS the first time, or
pre-seed the `fastembed_cache/`.

## 4. Configuration

```bash
cp .env.example .env
chmod 600 .env          # it holds DB credentials and model API keys
```

Fill in, at minimum:

- `DATABASE_URL` — **use `megh_app`**, not the `postgres` superuser. Create it
  first with [sql/01_create_megh_app_role.sql](sql/01_create_megh_app_role.sql)
  (see *Database roles and retention* below)
- `JWT_SECRET` — long random string; `python -c "import secrets;print(secrets.token_urlsafe(48))"`
- `ADMIN_TOKEN` — same, for the break-glass bootstrap endpoint
- the six model roles (`CLASSIFIER` / `SQL_GENERATION` / `RESPONSE` /
  `EMBEDDING` / `RERANKER` / `ASR` — each `*_BASE_URL` + `*_API_KEY`)
- `QDRANT_URL`, `QDRANT_COLLECTION`

> **`.env` is read by the application, not by systemd.** The unit deliberately
> does not use `EnvironmentFile=`: systemd keeps inline `#` comments as part of
> the value, so `MODEL_MAX_CONCURRENCY=24  # max in-flight` would arrive as the
> string `"24          # max in-flight"` and pydantic would refuse to start.
> pydantic-settings loads `.env` relative to `WorkingDirectory`.

Certificates: if the gateway's self-signed CA must be verified, place the bundle
at `certs/enlight-aiops-internal-ca.pem` and point `AI_MODEL_CA_BUNDLE_PATH` at
it. `certs/` is gitignored — the bundle is environment-specific.

## 5. Service

```bash
sudo cp deploy/systemd/megh-nlpservice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now megh-nlpservice
sudo systemctl status megh-nlpservice
journalctl -u megh-nlpservice -f
```

## 6. nginx

```bash
sudo cp deploy/nginx/nginx-nlpservice.conf /etc/nginx/sites-available/megh
sudo ln -sf /etc/nginx/sites-available/megh /etc/nginx/sites-enabled/megh
sudo nginx -t && sudo systemctl reload nginx
```

The config carries the OWASP edge hardening (TLS + HSTS, security headers,
per-IP `limit_req`/`limit_conn`, body/timeout caps, dotfile deny, `/admin` +
`/metrics` IP-allowlisting, real-client-IP recovery). Fill in every `# <<SET>>`
placeholder before `nginx -t`:

- `ssl_certificate` / `ssl_certificate_key` — real cert + key (Let's Encrypt via
  the `/.well-known/acme-challenge/` location already stubbed, or an ESDS-issued
  cert). The `limit_req_zone` / `set_real_ip_from` lines at the top must sit in
  an `http{}` context — keep them here if this file is included from `http{}`,
  else move them to `conf.d/`.
- `set_real_ip_from` — the vFirewall / LB subnet. Must match `TRUSTED_PROXIES`
  in `.env` so the app and nginx agree on the client IP.
- `server_name` — the real FQDN once a domain exists.
- the `allow … ; deny all;` CIDRs under `/admin`, `/admin-ui`, `/metrics`.

## 7. WAF — ModSecurity + OWASP CRS

Full runbook: [nginx/modsecurity/README.md](nginx/modsecurity/README.md).
Summary: `apt install libmodsecurity3 libnginx-mod-http-modsecurity`, lay down a
pinned CRS release plus the four config files from `deploy/nginx/modsecurity/`,
uncomment the two `modsecurity …` lines in the site config, reload. It starts in
`SecRuleEngine DetectionOnly` (logs, never blocks) — watch
`/var/log/nginx/modsec_audit.log` on real traffic for a week, add any
false-positive exclusions to `crs-overrides.conf`, then flip to `SecRuleEngine
On` and roll to the second VM. Rollback is one line + reload.

## 8. Verify

```bash
curl -s localhost:8400/health            # anonymous: {"status":"ok"} only
curl -s -H "X-Metrics-Token: $METRICS_TOKEN" localhost:8400/health    # + "components"
curl -s -H "X-Metrics-Token: $METRICS_TOKEN" localhost:8400/metrics   # or an admin JWT
curl -s -H "Authorization: Bearer <user JWT>" localhost:8400/api/rag/status   # KB point count
```

`/health` reports `degraded` with the failing component named when Postgres or
Qdrant is unreachable — the service still starts, by design, so a dependency
outage does not take the whole app down.

First start also: creates the `app.*` schema, seeds `app.users` from
`app/users.yaml` **only if that table is empty**, and ingests the scheme KB into
Qdrant (skipped when the collection is already populated).

---

## Database roles and retention

Two review-then-run scripts live in [sql/](sql/). Both need a superuser
(`postgres` / `dbadmin`) and are idempotent. Both were validated against the
live database inside a rolled-back transaction.

**[sql/01_create_megh_app_role.sql](sql/01_create_megh_app_role.sql)** — creates
`megh_app`: SELECT-only on `curated.*` and `semantic.*` (the surface generated
SQL runs against), read/write on `app.*` (the service's own users, sessions,
history and audit). Set a real password in the script first, then:

```bash
psql -d megh_db -f deploy/sql/01_create_megh_app_role.sql
# then in .env:
#   DATABASE_URL=postgresql+asyncpg://megh_app:<password>@10.48.242.4:5432/megh_db
sudo systemctl restart megh-nlpservice
```

> The existing `megh_readonly` cannot be used directly: it has no access to the
> `app` schema, so login, chat history and the audit trail would all fail.

**[sql/02_retention_policy.sql](sql/02_retention_policy.sql)** — adds
`app.purge_expired()`. `app.conversation_turns` holds every question, answer and
generated SQL; `app.query_audit` holds caller IPs. Nothing purges them today.
**The retention windows in that script are placeholders — set them from the
department's policy.** Then schedule it:

```bash
psql -d megh_db -f deploy/sql/02_retention_policy.sql
# crontab (postgres user):
15 3 * * *  psql -d megh_db -c "SELECT * FROM app.purge_expired();" >> /var/log/megh-purge.log 2>&1
```

---

## Before real traffic

- [ ] `DATABASE_URL` uses `megh_app` (sql/01), not the `postgres` superuser
- [ ] `JWT_SECRET` and `ADMIN_TOKEN` are real random values, not `CHANGE_ME`
      (startup logs a `CRITICAL` line otherwise — see `docs/SECURITY.md`)
- [ ] the seeded `CHANGE_ME` passwords are rotated (`superadmin` first)
- [ ] `ENV=prod` in `.env` (closes `/docs`, `/openapi.json`)
- [ ] TLS terminates in front of the service; `:80` redirects to `:443`
- [ ] `METRICS_TOKEN` set; `/admin` + `/metrics` `allow`/`deny` CIDRs filled in
- [ ] `TRUSTED_PROXIES` (`.env`) and `set_real_ip_from` (nginx) both match the vFirewall/LB subnet
- [ ] `curl -sI https://<host>/` shows the security headers (CSP, HSTS, nosniff, X-Frame-Options)
- [ ] ModSecurity + CRS installed and baked in `DetectionOnly`, then flipped to `On` (section 7)
- [ ] `.env` is `chmod 600` and owned by `ubuntu`
- [ ] retention windows set and `app.purge_expired()` scheduled (sql/02)
- [ ] `python tests/test_security.py` passes against the built venv

## Rollback

The unit is `Restart=always`. To roll back, replace the tree, then:

```bash
sudo systemctl restart megh-nlpservice
```

No migrations need reversing: the schema work is `CREATE TABLE IF NOT EXISTS`
plus `ADD COLUMN IF NOT EXISTS`, so an older build tolerates the newer schema.
