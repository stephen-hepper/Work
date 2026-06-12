# Deployment

How to run the ChemTreat lead generator + sales briefings on a single
VM with cron. Aimed at the simplest viable production setup: manual
file upload, local SQLite, weekly cron, and a single Azure OpenAI
deployment for the briefings LLM calls. Nothing else in the project
depends on Azure.

For local development, methodology, scoring rules, etc. see the
package READMEs:

- `chemtreat_water_leads/markdown/README.md` — methodology
- `chemtreat_water_leads/markdown/STARTING_GUIDE.md` — first-run walkthrough
- `chemtreat_water_leads/markdown/COMMANDS.md` — run patterns
- `chemtreat_sales_briefings/README.md` — briefings architecture

---

## What gets deployed

One VM hosts three workloads, all sharing the same SQLite file:

| Process | Cadence | Azure? | Network out |
|---|---|---|---|
| `chemtreat_water_leads.bulk_loader` | weekly + daily catch-up | no | `echo.epa.gov` (bulk zips), `echodata.epa.gov` (API fine-comb) |
| `chemtreat_water_leads.dump_run` | after each bulk | no | none |
| `chemtreat_sales_briefings.briefings` | weekly | **yes** | Azure OpenAI endpoint, SMTP server |

The viewer (`chemtreat_water_leads_viewer/index.html`) is a static
HTML file — open it in a browser locally with the materialized CSVs.
It isn't part of the VM deployment.

Snapshot.sqlite stays on the VM. No Snowflake. No managed identity. No
secret manager beyond `.env`. The simplification is intentional —
upgrade later if/when the requirements grow.

---

## Prerequisites

### VM

- **OS:** Ubuntu 22.04 LTS or Debian 12 (any recent Linux works; commands below assume apt)
- **Python:** 3.11+ (project uses standard library + `requests` + `openai`)
- **Disk:** ~5 GB free. Breakdown: ~2.2 GB EPA bulk cache + ~150 MB `snapshot.sqlite` after a few runs + room for materialized CSVs and logs.
- **CPU/RAM:** modest. The bulk_loader streams CSVs (peaks ~200 MB RAM); the briefings runner is mostly I/O.
- **Outbound network:** HTTPS to `echo.epa.gov`, `echodata.epa.gov`, your Azure OpenAI endpoint, and your SMTP server. EPA throttles aggressively per IP — if the VM shares an outbound IP with other heavy users of the ECHO API, expect more `lookup_failed` outcomes.

### Azure OpenAI (one-time, in the Azure portal)

1. Create an **Azure OpenAI** resource in your subscription. Pick a region with capacity for the model family you want.
2. Open **Azure OpenAI Studio → Deployments → Create new deployment**. Choose a base model (`gpt-4o` for quality, `gpt-4o-mini` for cost). Give the deployment a name — that's the value `AZURE_OPENAI_DEPLOYMENT` references at runtime, **not** the underlying model id. Common gotcha.
3. From the resource's **Keys and Endpoint** page, copy:
   - Key (either KEY 1 or KEY 2 — both work)
   - Endpoint URL — looks like `https://<resource-name>.openai.azure.com/`

A `gpt-4o-mini` deployment is enough to draft sensible briefings.
Cost estimate: a 5-region weekly run is typically a few cents
(`gpt-4o-mini`) to ~$1 (`gpt-4o`).

### SMTP credentials (only if `--send`)

Any SMTP server with auth works. For Gmail / Workspace, generate an
**App Password** (regular account password won't work with 2FA on).
Office 365 / Exchange can use Basic Auth (deprecated) or migrate to
Microsoft Graph SendMail — the briefings runner uses stdlib
`smtplib`, so it's Basic Auth today.

---

## One-time VM setup

```bash
# 1. System prereqs
sudo apt update
sudo apt install -y python3-venv python3-pip git

# 2. Project location (paths below assume /opt/chemtreat — pick whatever)
sudo mkdir -p /opt/chemtreat
sudo chown $USER:$USER /opt/chemtreat
cd /opt/chemtreat

# 3. Get the code. Either clone:
git clone <your repo URL> .
# Or scp from your laptop:
#   scp -r ~/PycharmProjects/Work/EPA user@vm:/opt/chemtreat/

# 4. Virtualenv + deps
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install requests openai

# 5. Log + cache directories
mkdir -p /opt/chemtreat/logs /opt/chemtreat/EPA/cache /opt/chemtreat/EPA/out
```

---

## Configuration: `EPA/.env`

Create `/opt/chemtreat/EPA/.env` with the production secrets. The
briefings runner reads this automatically via `_load_dotenv`. Shell
exports take precedence, so if you'd rather inject secrets via
systemd `EnvironmentFile=` or your own wrapper script that's fine
too — `.env` is the simplest path.

```bash
# Azure OpenAI — required for briefings
AZURE_OPENAI_API_KEY=<paste KEY 1 from portal>
AZURE_OPENAI_ENDPOINT=https://<resource-name>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<the deployment name you created>
# AZURE_OPENAI_API_VERSION=2024-10-21   # optional, defaults to this

# SMTP — only required when you pass --send
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=alerts@yourdomain.com
SMTP_PASSWORD=<app password, NOT account password>
BRIEFINGS_FROM_ADDRESS=alerts@yourdomain.com
```

Tighten permissions so other VM users can't read it:

```bash
chmod 600 /opt/chemtreat/EPA/.env
```

---

## Validation order

Run these once, in order, before turning on cron. Each step proves a
distinct piece works.

### 1. Bulk loader (no Azure, no LLM)

```bash
cd /opt/chemtreat/EPA
../.venv/bin/python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache
```

First run downloads ~2.2 GB of EPA bulk zips into `cache/`. Subsequent
runs reuse them for 7 days. Look for the end-of-run log line:

```
Run N outputs in out/bulk_nationwide_<stamp> (run_health.json, newly_snc_*.csv).
To materialize all_leads.csv + violation_events.csv for the viewer, run:
  python -m chemtreat_water_leads.dump_run --db ./snapshot.sqlite --run-id N --out ./materialized/run_N
```

Materialize the run and eyeball the CSVs:

```bash
../.venv/bin/python -m chemtreat_water_leads.dump_run --db ./snapshot.sqlite --latest
head ./materialized/run_<N>/all_leads.csv
```

### 2. Briefings smoke test (personal OpenAI key, no Azure yet)

Optional but cheap. Validates the tool-use loop + DB reads against
`api.openai.com` before Azure is wired up. Add `OPENAI_API_KEY=sk-...`
to `.env` (or shell-export it), then:

```bash
../.venv/bin/python -m chemtreat_sales_briefings.briefings \
    --openai-direct --regions Gulf -v
```

You should see tool calls logged + a markdown briefing printed to
stdout. Cost: a few cents. Delete the `OPENAI_API_KEY` from `.env`
once Azure is in place.

### 3. Briefings dry-run against Azure

Same prompt + tool surface, just routed to your Azure deployment:

```bash
../.venv/bin/python -m chemtreat_sales_briefings.briefings \
    --regions Gulf -v
```

If the env vars are wrong the script `sys.exit`s with a list of
what's missing — fail-fast before any tokens spent.

### 4. Briefings end-to-end (SMTP + state advance)

```bash
../.venv/bin/python -m chemtreat_sales_briefings.briefings \
    --send --mark-briefed
```

`--send` actually emails; `--mark-briefed` records the featured leads
in `briefings_state.sqlite` so they don't resurface next run. Confirm
the email landed at the configured recipient before turning on cron.

---

## Cron schedule

A reasonable starting point. Adjust times to whatever your sales team
finds useful (briefings should land in their inbox slightly before
they start their week).

```cron
# crontab -e

# Weekly nationwide bulk + materialize (Mondays 04:00 UTC)
0 4 * * 1   cd /opt/chemtreat/EPA && \
            /opt/chemtreat/.venv/bin/python -m chemtreat_water_leads.bulk_loader \
              --out ./out --db ./snapshot.sqlite --cache ./cache \
              >> /opt/chemtreat/logs/bulk.log 2>&1 && \
            /opt/chemtreat/.venv/bin/python -m chemtreat_water_leads.dump_run \
              --db ./snapshot.sqlite --latest \
              >> /opt/chemtreat/logs/dump.log 2>&1

# Daily catch-up for backoff-eligible lookups (Tue-Fri 02:00 UTC).
# The per-row drill-down backoff (6h -> 24h -> 7d) makes this safe —
# leads still in their cooldown window are no-ops. Drops the
# `lookup_failed` count on previously-blocked leads as EPA's throttle
# clears.
0 2 * * 2-5 cd /opt/chemtreat/EPA && \
            /opt/chemtreat/.venv/bin/python -m chemtreat_water_leads.bulk_loader \
              --out ./out --db ./snapshot.sqlite --cache ./cache \
              >> /opt/chemtreat/logs/bulk.log 2>&1

# Weekly briefings (Mondays 07:00 UTC — well after the bulk run completes)
0 7 * * 1   cd /opt/chemtreat/EPA && \
            /opt/chemtreat/.venv/bin/python -m chemtreat_sales_briefings.briefings \
              --send --mark-briefed \
              >> /opt/chemtreat/logs/briefings.log 2>&1
```

Notes:

- **Run as a regular user**, not root. `crontab -e` from the user that
  owns `/opt/chemtreat/`.
- **`>> ... 2>&1`** captures both stdout and stderr so a silent
  failure (Azure key expired, EPA renamed a bulk file) lands in the
  log. Consider piping these through `logger` if you want them in
  syslog / journald.
- **No `sleep` between the bulk and dump steps** — they're chained
  with `&&`, so dump only fires when bulk completes cleanly. If bulk
  fails (network glitch, EPA outage), dump doesn't fire and the
  next day's catch-up bulk takes over.
- **Timezone:** `cron` uses the system timezone. Confirm with `date`
  before pinning times.

---

## Maintenance

### Log rotation

Cron will fill `/opt/chemtreat/logs/` indefinitely. Easiest fix:
drop a `/etc/logrotate.d/chemtreat` config:

```
/opt/chemtreat/logs/*.log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    create 0640 chemtreat chemtreat
}
```

(Adjust user/group to whoever owns the directory.)

### `snapshot.sqlite` growth

The DB grows by ~50–100 MB per nationwide bulk run (one row in the
membership tables per touched key). Long-term, this gets large but
not painful — a year of weekly runs is single-digit GB.

If you want to prune old membership rows once they're no longer
useful for "first-seen" diffs, the pattern is documented in
`SNOWFLAKE_DESIGN.md`:

```sql
DELETE FROM run_facility_membership
 WHERE run_id IN (SELECT run_id FROM runs WHERE run_at < datetime('now', '-1 year'));
DELETE FROM run_violation_membership
 WHERE run_id IN (SELECT run_id FROM runs WHERE run_at < datetime('now', '-1 year'));
VACUUM;
```

Don't run this routinely — keep at least a year so seasonal patterns
still surface in the diffs.

### Materialized CSV cleanup

`dump_run` writes to `./materialized/run_<id>/`. These don't get
GC'd automatically. Either:

- Delete by hand when sales is done with them.
- Cron a weekly `find ./materialized -mindepth 1 -mtime +30 -type d -exec rm -rf {} +`.

### Cache invalidation

EPA bulk zips refresh weekly. The bulk loader cache window is 7 days,
so files older than that are re-downloaded automatically. If you
suspect a bad cache (sudden zero-event run after a weekly refresh):

```bash
rm -rf /opt/chemtreat/EPA/cache/
```

Next run re-downloads everything (~2.2 GB, ~5–10 min on a decent link).

### Updating the code

```bash
cd /opt/chemtreat
git pull   # or scp the changed files from your laptop
# No service restart — cron will pick up the new code on the next fire.
```

If you add a runtime dep, `pip install` it into the venv before the
next cron fire.

---

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing env: AZURE_OPENAI_API_KEY, ...` | `.env` not in `EPA/` or VM cron doesn't `cd` there | Confirm `cd /opt/chemtreat/EPA` is in the cron line BEFORE the python invocation |
| Briefings: `openai.NotFoundError: The API deployment for this resource does not exist.` | `AZURE_OPENAI_DEPLOYMENT` set to model id instead of deployment name | Use the deployment name from Azure OpenAI Studio, not `gpt-4o` |
| Bulk: `... high-value leads STILL no_events — API retries exhausted` | EPA throttled the VM's IP | Expected. The gate skips RNC-only / terminated / no-violation; the per-row backoff (6h → 24h → 7d) will retry actionable leads on the daily catch-up cron. Check Run Health tab in the viewer for the `lookup_failed_by_state` breakdown. |
| Bulk: zip download 404 | EPA renamed a bulk file | Check <https://echo.epa.gov/tools/data-downloads> and update `BULK_URLS` in `bulk_loader.py` — see MEMORY.md "Bulk loader: the nationwide path" |
| SMTP: `(535, b'5.7.8 Username and Password not accepted')` | Gmail with regular password instead of App Password | Generate an App Password in Google Account → Security → 2-Step Verification → App Passwords |
| `snapshot.sqlite is locked` mid-run | Two pipeline processes running against the same DB | Don't run bulk_loader concurrently — cron jobs above are serial by schedule, but watch out if you trigger manual runs while cron is firing |

---

## Upgrade paths (when complexity is actually justified)

These are documented elsewhere; deferred deliberately for this
simple deployment:

- **Azure Key Vault for secrets** — swap `.env` for `DefaultAzureCredential` + key vault lookups when you outgrow shared `.env` files.
- **Snowflake as the state store** — replaces SQLite when the DB outgrows the VM or you want multi-host writers. Full design in `chemtreat_water_leads/markdown/SNOWFLAKE_DESIGN.md`.
- **Azure Container Apps Jobs** — replaces the bare-VM-plus-cron pattern when you want declarative infra + autoscale. The code doesn't change; the cron schedule becomes a Job trigger.
- **Managed identity for SMTP** — switch to Microsoft Graph SendMail or Azure Communication Services when shared mailboxes outgrow plain SMTP auth.

For now, none of these are needed. A VM + cron + a single Azure
OpenAI deployment + `.env` covers the end-to-end use case at minimum
infrastructure cost.
