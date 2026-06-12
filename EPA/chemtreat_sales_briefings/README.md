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
├── regions.py     ← region → states → sales lead config (edit freely)
└── state.py       ← briefings_state.sqlite — "which leads featured when"
```

State lives in a separate `briefings_state.sqlite` (default location:
`EPA/briefings_state.sqlite`, alongside `snapshot.sqlite`) so this
package never writes to the pipeline's snapshot DB. The snapshot is
opened `mode=ro` everywhere it's read; the state DB is opened
read-only for the candidate JOIN and read-write only by
`state.record_briefing_run`.

Single-file main on purpose. When it grows past ~600 lines or a second
consumer shows up, split `briefings.py` into `tools.py` + `llm.py` +
`email.py`.

---

## Quick test with a personal OpenAI key

Before Azure is provisioned, you can validate the whole flow against
api.openai.com with a personal key. Copy `.env.example` to `EPA/.env`,
fill in `OPENAI_API_KEY`, and pass `--openai-direct`:

```bash
cd ~/PycharmProjects/Work/EPA
cp chemtreat_sales_briefings/.env.example .env
# edit .env, paste your sk-... key into OPENAI_API_KEY

../.venv/bin/python -m chemtreat_sales_briefings.briefings \
    --openai-direct --regions Gulf -v
```

That produces a one-region dry-run briefing using `gpt-5.4-nano`
(override with `OPENAI_MODEL=gpt-5.4` in `.env` if you want stronger
prose). The same tool surface and prompts as the Azure path — the
only thing that changes is the transport. `EPA/.env` is gitignored;
shell-exported env vars still take precedence so you can also just
`export OPENAI_API_KEY=...` and skip the file.

Cost ballpark with `gpt-5.4-nano`: a one-region dry-run is typically
a few cents at current pricing. Five regions ≈ $0.10-$0.30 depending
on how much tool data the model pulls. Cheap enough to iterate on
tone freely.

---

## Running it (Azure — the production path)

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

# Mark featured leads so they don't re-surface next run (independent
# of --send so you can mark dry-runs or send without marking)
../.venv/bin/python -m chemtreat_sales_briefings.briefings --mark-briefed

# Tune how many candidates per region per run
../.venv/bin/python -m chemtreat_sales_briefings.briefings --leads-per-region 15

# Force a re-brief (ignore prior briefing state — testing only)
../.venv/bin/python -m chemtreat_sales_briefings.briefings --force-rebrief
```

Both DB paths default to the current working directory — `snapshot.sqlite`
and `briefings_state.sqlite`. That assumes you run from `EPA/` (per the
examples above). Override with `--db` or `--state-db` if your layout
differs.

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

## Volume control & the state DB

The pipeline tracks ~40K leads. Briefing all of them is neither
useful (sales can't read 40K bullets) nor honest (most haven't moved
since last week). Two mechanisms keep briefings focused:

### 1. The `briefing_candidates` tool is the LLM's primary feature source

It returns leads that warrant a fresh briefing right now — not the
top-N by score. A lead qualifies as a candidate when **any one** of:

| `briefing_status` | When it fires |
|---|---|
| `never_briefed` | No row in `lead_briefings` for this `(registry_id, program)`. First-time leads always surface. |
| `score_changed` | Current `lead_score` differs from `lead_score_at_brief` by more than 5. Surfaces leads whose situation shifted. The 5-point threshold matches the design intent of catching meaningful moves while not flapping on noise. |
| `new_activity` | `facilities.last_seen` is newer than `lead_briefings.last_featured_at`. The bulk pipeline touched this lead after we last briefed it — usually means new events or refreshed signals. |

Each candidate row carries `briefing_status` so the LLM's prose can
frame correctly: a `never_briefed` lead reads as a fresh find, a
`score_changed` lead reads as an update ("score jumped 142 → 168"
using the included `prior_lead_score`), a `new_activity` lead reads
as a refresh.

The system prompt steers the LLM toward `briefing_candidates` and
explicitly tells it to acknowledge an empty result rather than fall
back to `top_leads` to fill space. `top_leads` and the other tools
remain available as supplements.

### 2. `--leads-per-region` is a hard cap on candidate count

Default 10. Enforced inside `tool_briefing_candidates` — the LLM can
ask for more, but the cap clamps the response. So even when the
candidate pool is wide (e.g. immediately after a major run with lots
of new activity), the LLM sees at most N leads per region per call.

### Marking is independent of sending

| Flag | Behavior |
|---|---|
| no flags | dry-run, no marking — pure iteration mode |
| `--send` | actually email, no marking — useful when you want to send the same briefing again next run |
| `--mark-briefed` | mark featured leads, no email — useful for advancing the candidate pool during testing |
| `--send --mark-briefed` | the production combo — email and advance state |
| `--force-rebrief` | bypass the candidate filter entirely; return top leads by score regardless of prior brief. For testing; doesn't interact with marking. |

The marking writes both a `briefing_runs` row (audit log) and one
`lead_briefings` row per featured lead (state). The `lead_score` and
`last_seen` at briefing time get recorded so the next run's
candidate predicate can compute deltas.

### Schema (briefings_state.sqlite)

```sql
CREATE TABLE briefing_runs (
    briefing_run_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at           TEXT NOT NULL,
    region           TEXT NOT NULL,
    mode             TEXT NOT NULL,         -- 'dry_run' or 'send'
    lead_count       INTEGER NOT NULL
);

CREATE TABLE lead_briefings (
    registry_id           TEXT NOT NULL,
    program               TEXT NOT NULL,
    last_featured_at      TEXT NOT NULL,
    last_featured_run_id  INTEGER,          -- FK to briefing_runs
    lead_score_at_brief   INTEGER,
    last_seen_at_brief    TEXT,
    region                TEXT,
    PRIMARY KEY (registry_id, program)
);
```

The candidate query LEFT JOINs `facilities` (in `snapshot.sqlite`)
with `lead_briefings` (in `briefings_state.sqlite`) via
`ATTACH DATABASE` — both opened read-only for the read path. The
write path uses a separate read-write connection to the state DB
only.

### Caveat — what gets marked

The script marks **every lead returned by `briefing_candidates`**,
not just the ones the LLM literally cited in the briefing prose. So
if the cap returns 10 candidates and the LLM features 6 in the
final write-up, all 10 are marked. This slightly over-marks but
keeps the script simple. If you want only-actually-featured marking,
swap the post-run write for an LLM tool call like `submit_briefing`
that the model invokes with the explicit registry_ids it included.

---

## Tool reference

What the LLM can call. Every tool returns JSON-serialisable data; arrays
of facility rows include a curated set of high-signal columns to keep
the conversation lean.

| Tool | Args | Returns |
|---|---|---|
| `list_regions` | — | All region names. |
| `region_summary` | `region` | Leads-per-program, leads-per-outreach-posture, signal-tag fires across the region. The "orient yourself" call. |
| **`briefing_candidates`** | `region`, `limit?`, `min_score?` (default 50) | **Primary feature source.** Leads warranting a fresh briefing — never-briefed, score moved by >5, or new bulk activity since last briefing. Capped by `--leads-per-region`. Each row carries `briefing_status` and (for `score_changed`) `prior_lead_score`. |
| `top_leads` | `region`, `limit?` (≤100), `min_score?` (default 50) | Top-scored facilities ignoring briefing history. Use only for supplementary context — `briefing_candidates` is the main feature source. |
| `newly_seen` | `region`, `since_days?` (1-90, default 14), `limit?`, `min_score?` (default 30) | Facilities first seen in the last N days. Supplementary to `briefing_candidates`. |
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
- **No persistence of past briefing prose.** `briefings_state.sqlite`
  records *which leads were featured when*, not the rendered text. If
  the model needs to read back "what did we say about facility X last
  week," add a `briefings_text` table and a `recent_briefing` tool.
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

Two useful tests that don't burn tokens:

```bash
cd ~/PycharmProjects/Work/EPA

# 1. Read-only: confirm region_summary returns sane numbers
../.venv/bin/python -c "
from chemtreat_sales_briefings.briefings import tool_region_summary
from pathlib import Path
import json
ctx = {'db_path': Path('snapshot.sqlite')}
print(json.dumps(tool_region_summary(ctx, {'region': 'Gulf'}),
                 indent=2, default=str))
"

# 2. State + candidates: confirm gating works end-to-end against a
#    throwaway state DB
../.venv/bin/python -c "
from chemtreat_sales_briefings import state, briefings
from pathlib import Path
state_path = Path('/tmp/test_briefings_state.sqlite')
state_path.unlink(missing_ok=True)
ctx = {
    'db_path': Path('snapshot.sqlite'),
    'state_path': state_path,
    'leads_per_region': 5,
    'force_rebrief': False,
    'candidates_tracker': {},
}
# First call: 5 candidates, all never_briefed
r = briefings.tool_briefing_candidates(ctx, {'region': 'Gulf'})
print(f'first call: {len(r[\"candidates\"])} candidates')
for c in r['candidates'][:3]:
    print(f'  {c[\"company\"]} ({c[\"state\"]}) status={c[\"briefing_status\"]}')
# Mark them, then second call should return the NEXT 5
state.record_briefing_run(state_path, 'Gulf', 'dry_run',
                          ctx['candidates_tracker']['Gulf'])
ctx['candidates_tracker'] = {}
r2 = briefings.tool_briefing_candidates(ctx, {'region': 'Gulf'})
print(f'after marking: {len(r2[\"candidates\"])} candidates (next batch)')
state_path.unlink(missing_ok=True)
"
```

The first exercises the read-only DB path. The second exercises the
candidate filter end-to-end against a throwaway state DB — proves the
JOIN works, the predicate fires correctly, and marking advances the
pool. No Azure required for either.
