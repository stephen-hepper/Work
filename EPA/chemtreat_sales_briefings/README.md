# chemtreat_sales_briefings

Regional sales briefings drafted by an LLM that queries
`snapshot.sqlite` through a fixed, narrow tool surface.

This package is **separate from `chemtreat_water_leads/`** on purpose:
that package owns data ingest, scoring, and the snapshot DB; this
package owns regional outreach drafting and email delivery. The
contract between them is `snapshot.sqlite` — this package only reads it
(opens with `mode=ro`), never writes.

---

## What it does

For each region defined in `regions.py`, an LLM:

1. Asks the database (through the narrow tools) what's worth attention
   this week — top leads by score, fresh signal, active SNC.
2. Drafts a markdown briefing aimed at the region's sales leader.
3. Optionally emails it (default is dry-run → stdout).

One run, one process, one Python script. No MCP server, no separate
service, no shared infra.

---

## Why direct function-calling, not MCP or raw SQL

Two design choices need separating:

### Narrow tools, not raw SQL

The LLM does **not** get a `run_sql` tool. Every database read goes
through a fixed `_query_*` function in `briefings.py` with parameterised
binds. The LLM picks the tool name and supplies arguments; the script
validates the arguments and runs a fixed query template. There is no
path from a model token to an executed SQL string.

This is the deterministic-control guarantee: the surface the LLM sees
is the surface we authored. Argument values that look weird produce a
structured `{"error": "..."}` response the model can react to, not a
crash and not unexpected data exfiltration.

### Direct (Anthropic-style function-calling) vs MCP

We chose direct because there's exactly one consumer right now (this
script, eventually on a cron). MCP earns its keep when the same tool
surface needs to serve multiple LLM clients (Claude Desktop / a custom
app / etc.) — the upfront work pays off in reuse. With a single
consumer, MCP adds a server process and a protocol layer for no benefit.

If a future need arises to ask ad-hoc questions interactively
("what are the top TX leads with score > 100?"), the same tool
functions in `briefings.py` are portable: wrap them in an MCP server
without rewriting the SQL or rethinking the schema.

---

## File layout

```
chemtreat_sales_briefings/
├── __init__.py
├── README.md      ← you are here
├── briefings.py   ← tools + LLM loop + email render/send + CLI
└── regions.py     ← region → states → sales lead config (edit freely)
```

Single-file main on purpose. When it grows past ~600 lines or a second
consumer shows up, split `briefings.py` into `tools.py` + `llm.py` +
`email.py`.

---

## Running it

The CLI defaults to **dry-run** — every region's briefing prints to
stdout, no emails sent. This is the safe iteration mode.

```bash
cd ~/PycharmProjects/Work/EPA

# Dry run, all regions
../.venv/bin/python -m chemtreat_sales_briefings.briefings

# One region
../.venv/bin/python -m chemtreat_sales_briefings.briefings --regions Gulf

# Several regions
../.venv/bin/python -m chemtreat_sales_briefings.briefings --regions Gulf,Southeast

# Verbose (logs every tool call the LLM makes)
../.venv/bin/python -m chemtreat_sales_briefings.briefings --regions Gulf -v

# Actually send via SMTP (requires SMTP env vars below)
../.venv/bin/python -m chemtreat_sales_briefings.briefings --send
```

The DB path defaults to `../snapshot.sqlite` (one level above this
package), matching the layout in `EPA/`. Override with `--db`.

---

## Environment variables

### Required for any run (LLM)

| Variable | Notes |
|---|---|
| `AZURE_OPENAI_API_KEY` | From the Azure OpenAI resource. |
| `AZURE_OPENAI_ENDPOINT` | e.g. `https://my-resource.openai.azure.com/`. |
| `AZURE_OPENAI_DEPLOYMENT` | The **deployment name** you configured in Azure (not the underlying model name). |

### Optional (LLM)

| Variable | Default | Notes |
|---|---|---|
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Bump when Azure releases newer GA versions. |

### Required only with `--send` (SMTP)

| Variable | Notes |
|---|---|
| `SMTP_HOST` | e.g. `smtp.gmail.com`. |
| `SMTP_USER` | The account sending the briefing. |
| `SMTP_PASSWORD` | App password (Gmail) or account password (most other providers). |
| `BRIEFINGS_FROM_ADDRESS` | Optional `From:` header. Defaults to `noreply@example.com`. |
| `SMTP_PORT` | Defaults to `587` (STARTTLS). |

The script validates SMTP env upfront when `--send` is passed, so a
missing credential fails fast — before any LLM tokens are spent.

---

## Region configuration

`regions.py` is a flat Python dict — edit it directly. Each region maps
to `states` (USPS codes) and a `lead` (name + email):

```python
REGIONS = {
    "Gulf": {
        "states": ["TX", "LA", "OK", "AR", "MS", "AL"],
        "lead": {"name": "<Gulf Sales Lead>", "email": "gulf-lead@example.com"},
    },
    ...
}
```

Region names are the LLM-facing identity. The LLM picks a region name;
the script resolves it to states inside every tool. The LLM **cannot**
widen its scope — naming an unknown region returns a structured error,
not a free-form SQL query.

To add a region, append an entry. To split one (e.g. carve TX off the
Gulf into its own territory), edit the state lists. No code changes
elsewhere.

---

## Tool reference

What the LLM can call. Every tool returns JSON-serialisable data; arrays
of facility rows include a curated set of high-signal columns to keep
the conversation lean.

| Tool | Args | Returns |
|---|---|---|
| `list_regions` | — | All region names. |
| `region_summary` | `region` | Leads-per-program, leads-per-outreach-posture, signal-tag fires across the region. The "orient yourself" call. |
| `top_leads` | `region`, `limit?` (≤100), `min_score?` (default 50) | Top-scored facilities in the region, with score, reasons, posture, and signal tags. |
| `newly_seen` | `region`, `since_days?` (1-90, default 14), `limit?`, `min_score?` (default 30) | Facilities first seen in the last N days. The "fresh signal" call. |
| `active_snc` | `region`, `limit?` | Currently-flagged Significant Non-Compliers. |
| `lead_detail` | `registry_id`, `program` | Full row for one facility, including pre-violation signals and the ECHO URL. |
| `violation_events` | `registry_id`, `program`, `limit?` (≤50, default 10) | Per-event violation rows — CWA carries per-DMR parameter / limit / exceedance %; SDWA carries category + status. |

Tool descriptions seen by the model live in `TOOL_SCHEMAS` (JSON schema
for parameters + a natural-language description). When you add a tool,
write its description like you're onboarding the LLM — what it's for
and when to reach for it, not just what it returns.

Errors come back as `{"error": "<type>: <msg>"}` so the model can react
to a bad argument (typo'd region, missing column) without the loop
crashing. Same applies to unknown registry_ids.

---

## How the tool-use loop works

```
system prompt + user prompt for region X
  → LLM picks tools, gets data
  → keeps iterating tool calls until it has enough
  → emits the final briefing as plain text/markdown
```

Hard cap: `MAX_TURNS = 20`. A briefing should comfortably finish in
5-10 turns; anything past 20 is the model spinning. The cap raises a
runtime error rather than silently producing a partial brief.

`temperature=0.3` keeps the prose stable across runs while leaving
enough variability to feel hand-written rather than mechanical. Drop
to `0.0` if you need byte-identical reproducibility for testing.

The full system prompt lives at the top of `briefings.py` as
`SYSTEM_PROMPT` — adjust there to change tone, length, or structure.
The per-region `build_user_prompt` adds the date and sales lead's name
so each region's briefing is addressed correctly.

---

## What this intentionally doesn't do

- **No HTML email.** Plaintext / markdown only. Easier to debug, reads
  fine in any client. Upgrade path: swap `msg.set_content` for a
  multipart payload with an HTML alternative.
- **No persistence of past briefings.** Each run is independent. If
  the model needs context like "what did we send last week," add a
  `recent_briefings` tool backed by a simple JSON log.
- **No CRM integration.** The output is email. Wire to Salesforce /
  HubSpot when sales asks for it.
- **No retry / queue.** A failed SMTP send raises and bails on the
  region. Add at-most-once or at-least-once semantics when you decide
  which one fits.
- **No write access to `snapshot.sqlite`.** The DB is opened
  `mode=ro`. The pipeline owns writes; this package only reads.

---

## Roadmap (informal)

- Wire a daily/weekly cron once dry-run output looks good for a few
  iterations. Schedule via launchd or a cloud cron — same as the bulk
  loader pattern (see `EPA/chemtreat_water_leads/markdown/COMMANDS.md`).
- Add a `score_jumped` tool once the snapshot grows a score-history
  table. Today it'd require cross-run joins the schema doesn't support
  cleanly.
- Add an HTML render path when the recipient list is past 5-10 leads
  per briefing — long markdown reads worse than a structured HTML
  table at that size.
- Consider an MCP wrapper if the same tools start being useful for
  interactive ad-hoc queries via Claude Desktop or a custom app. The
  query functions stay; only the transport changes.

---

## Quick smoke test (no LLM call)

To verify the tool layer reads the DB correctly without spending
tokens:

```bash
cd ~/PycharmProjects/Work/EPA
../.venv/bin/python -c "
from chemtreat_sales_briefings.briefings import tool_region_summary
from pathlib import Path
import json
print(json.dumps(tool_region_summary(Path('snapshot.sqlite'),
                                     {'region': 'Gulf'}),
                 indent=2, default=str))
"
```

That exercises `_open_db`, the state resolution, and one
`_query_region_summary` against your real snapshot — no Azure
required.
