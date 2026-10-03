# WAF — ModSecurity v3 + OWASP Core Rule Set

Edge web-application firewall for nlp-service. Sits in nginx, in front of the
FastAPI app, and enforces the OWASP CRS. This is **defence in depth** — the app
already validates its own input (read-only SQL guard, SELECT-only DB role,
length caps, security headers); the WAF is the outer layer that catches generic
attack traffic before it reaches the app at all.

Ships **disabled and in log-only mode** — turning it on is two deliberate steps
(install, then flip `SecRuleEngine`), described below.

## Files in this directory

| File | Role |
| --- | --- |
| `main.conf` | entry point — `Include`d from `nginx-nlpservice.conf`. Chains the four pieces below in the right order. |
| `modsecurity.conf` | engine config (mode, body parsing, audit log). Curated copy of the upstream recommended file — `CHANGED-FOR-MEGH` marks every deviation. |
| `crs-setup-overrides.conf` | paranoia level (PL1), anomaly thresholds, allowed methods / content types. Loaded after the CRS's own `crs-setup.conf`. |
| `crs-overrides.conf` | **the tuning that matters** — narrow, path-scoped exclusions so the SQLi/XSS families don't block legitimate natural-language questions on `/api/query`. Loaded last. |

## 1. Install (Ubuntu 22.04 / 24.04, per application VM)

```bash
sudo apt-get update
sudo apt-get install -y libmodsecurity3 libnginx-mod-http-modsecurity

# CRS — pin a release, don't track master.
CRS_VER=4.10.0
curl -fsSL -o /tmp/crs.tar.gz \
  https://github.com/coreruleset/coreruleset/archive/refs/tags/v${CRS_VER}.tar.gz
sudo mkdir -p /etc/nginx/modsecurity
sudo tar -xzf /tmp/crs.tar.gz -C /etc/nginx/modsecurity
sudo mv /etc/nginx/modsecurity/coreruleset-${CRS_VER} /etc/nginx/modsecurity/crs
sudo cp /etc/nginx/modsecurity/crs/crs-setup.conf.example \
        /etc/nginx/modsecurity/crs/crs-setup.conf

# unicode mapping file the engine needs
sudo curl -fsSL -o /etc/nginx/modsecurity/unicode.mapping \
  https://raw.githubusercontent.com/owasp-modsecurity/ModSecurity/v3/master/unicode.mapping

# lay our config down
sudo cp deploy/nginx/modsecurity/{main.conf,modsecurity.conf,crs-setup-overrides.conf,crs-overrides.conf} \
        /etc/nginx/modsecurity/

sudo touch /var/log/nginx/modsec_audit.log
sudo chown www-data:adm /var/log/nginx/modsec_audit.log
```

Make sure the dynamic module loads — in `/etc/nginx/nginx.conf`, top level:

```nginx
load_module modules/ngx_http_modsecurity_module.so;
```

## 2. Enable in the site config

In `deploy/nginx/nginx-nlpservice.conf` (already deployed to
`/etc/nginx/sites-available/megh`), uncomment the two lines in the `server {}`
block:

```nginx
modsecurity on;
modsecurity_rules_file /etc/nginx/modsecurity/main.conf;
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

At this point the engine is running but in **`DetectionOnly`** — it logs, it
never blocks. Verify:

```bash
curl -sk 'https://localhost/?x=%3Cscript%3Ealert(1)%3C/script%3E' -o /dev/null
sudo tail -n 40 /var/log/nginx/modsec_audit.log      # should show a 941xxx hit
```

## 3. Bake-in (1–2 weeks of real traffic)

Watch `modsec_audit.log`. For every entry caused by a **legitimate** request,
add a scoped exclusion to `crs-overrides.conf` (copy the pattern already there),
reload, repeat. Useful triage:

```bash
# which rule ids are firing, most frequent first
sudo grep -oE '\[id "[0-9]+"\]' /var/log/nginx/modsec_audit.log | sort | uniq -c | sort -rn

# full detail for one transaction id
sudo awk -v id=XXXXXXXX '$0 ~ id' /var/log/nginx/modsec_audit.log
```

Natural-language questions to `/api/query` should produce **no** hits once the
exclusions in `crs-overrides.conf` are in place — test with e.g.
`{"question": "show the drop in expenditure for delete-heavy districts; union of both schemes"}`.

## 4. Cutover to blocking

When the audit log has been free of false positives on real traffic for a few
days:

1. `crs-overrides.conf` — no pending exclusions to add.
2. `modsecurity.conf` — change `SecRuleEngine DetectionOnly` → `SecRuleEngine On`.
3. `sudo nginx -t && sudo systemctl reload nginx`
4. Smoke test: the `<script>` probe from step 2 now returns `403`; a normal
   portal load and a normal `/api/query` still return `200`.
5. Roll to the second VM.

**Rollback** is instant: set `SecRuleEngine DetectionOnly` (or comment the two
`modsecurity` lines) and reload.

## 5. Log rotation

`/etc/logrotate.d/nginx` already covers `/var/log/nginx/*.log`. Confirm
`modsec_audit.log` is picked up; it can grow fast in `DetectionOnly` on noisy
days.

## Upgrading

- **CRS**: bump `CRS_VER`, re-extract, re-copy `crs-setup.conf`, keep our four
  files, `nginx -t`, reload. Re-check the audit log for new FPs.
- **libmodsecurity**: `apt upgrade`, then diff the new
  `modsecurity.conf-recommended` against our `modsecurity.conf` and re-apply the
  `CHANGED-FOR-MEGH` deltas.
