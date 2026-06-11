"""Regional sales briefings: query snapshot.sqlite via narrow tools,
let Azure OpenAI draft a briefing per region, optionally email it,
and track which leads have been featured so the next run doesn't
re-feature the same ones.

Architecture
------------
The LLM cannot author SQL. Every database read goes through one of the
fixed `_query_*` functions below, each backed by a parameterised query
with bind variables. We expose these to the LLM as tool definitions
(OpenAI function-calling shape). The model picks a tool name and supplies
arguments; the script validates the arguments, runs the fixed query,
returns JSON. There is no `run_sql` tool.

Region names are the LLM-facing identity for territory; the mapping to
state lists is enforced inside this script (regions.py). The LLM cannot
widen its scope past what regions.py defines.

Volume control
--------------
The `briefing_candidates` tool is the primary "what to write about"
call — it returns ONLY leads that warrant a fresh briefing
(never-briefed, score moved by > 5 since last briefing, or fresh EPA
activity since last briefing). The cap on candidates per region is
enforced inside the tool by `--leads-per-region` (default 10), so the
LLM cannot exceed it. Briefing state (which lead was featured when)
lives in a separate `briefings_state.sqlite` so the briefings package
never writes to snapshot.sqlite — the pipeline's read-only contract is
preserved.

Run with `--dry-run` (default) to print to stdout. `--send` enables the
SMTP path. Marking leads as "featured" is controlled separately via
`--mark-briefed` — pass it (with --send or stand-alone) when you want
the candidate pool to shrink for the next run. `--force-rebrief`
bypasses the candidate filter for testing.

See README.md for the full design rationale, env vars, and tool reference.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from . import regions, state


log = logging.getLogger("chemtreat.briefings")

# Hard cap on tool-use turns. A briefing should comfortably finish in
# 5-10 turns; anything beyond this is the model spinning, not working.
MAX_TURNS = 20

# Columns we return from `facilities` for any list-of-leads tool. Kept
# narrow on purpose — too much per row inflates the payload and makes
# the LLM's life harder. The expand path is `lead_detail` for one row
# at a time when the model wants more.
_LEAD_LIST_COLUMNS = (
    "registry_id", "program", "company", "city", "state", "naics",
    "lead_score", "outreach_posture", "score_reasons",
    "tag_active_snc", "tag_treatment_technique", "tag_mcl_violation",
    "tag_lead_copper", "tag_chemtreat_high_relevance",
    "tag_exceeds_treatable_parameter", "tag_treatable_permit",
    "tag_discharges_to_impaired", "tag_recent_exceedance",
)


# ---------------------------------------------------------------- DB layer

@contextmanager
def _open_db(db_path: Path):
    """Read-only connection. We never write — the briefings tool is a
    pure consumer of the snapshot the pipeline produces."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _rows_to_dicts(rows, tag_cols: tuple[str, ...] = ()) -> list[dict]:
    """Turn sqlite3.Row results into plain dicts, coercing tag_*
    integers to booleans so the JSON we send the LLM is shaped like
    real semantics, not storage details."""
    out = []
    tag_set = set(tag_cols)
    for r in rows:
        d = {k: r[k] for k in r.keys() if r[k] is not None}
        for k in list(d.keys()):
            if k in tag_set:
                d[k] = bool(d[k])
        out.append(d)
    return out


def _state_placeholders(states: list[str]) -> str:
    """`?,?,?` of the right arity, for use with IN clauses."""
    return ",".join("?" for _ in states)


# ---------------------------------------------------------------- queries
#
# Each function below is one fixed query template. The tool layer above
# does no string formatting — arguments are only ever bind variables.
# Adding a new tool means adding one function here and one entry in
# `TOOL_SCHEMAS` + `TOOL_HANDLERS` below.

_LEAD_LIST_SELECT = ", ".join(_LEAD_LIST_COLUMNS)
_LEAD_TAG_COLS = tuple(c for c in _LEAD_LIST_COLUMNS if c.startswith("tag_"))


def _query_top_leads(conn, states: list[str], limit: int,
                     min_score: int) -> list[dict]:
    sql = (
        f"SELECT {_LEAD_LIST_SELECT} FROM facilities "
        f"WHERE state IN ({_state_placeholders(states)}) "
        f"  AND lead_score >= ? "
        f"ORDER BY lead_score DESC "
        f"LIMIT ?"
    )
    rows = conn.execute(sql, [*states, min_score, limit]).fetchall()
    return _rows_to_dicts(rows, _LEAD_TAG_COLS)


def _query_newly_seen(conn, states: list[str], since_iso: str,
                      limit: int, min_score: int) -> list[dict]:
    """Facilities first observed since `since_iso`. ISO timestamps sort
    lexically — no date math in SQL."""
    sql = (
        f"SELECT {_LEAD_LIST_SELECT}, first_seen FROM facilities "
        f"WHERE state IN ({_state_placeholders(states)}) "
        f"  AND first_seen >= ? "
        f"  AND lead_score >= ? "
        f"ORDER BY lead_score DESC "
        f"LIMIT ?"
    )
    rows = conn.execute(
        sql, [*states, since_iso, min_score, limit]
    ).fetchall()
    return _rows_to_dicts(rows, _LEAD_TAG_COLS)


def _query_active_snc(conn, states: list[str], limit: int) -> list[dict]:
    sql = (
        f"SELECT {_LEAD_LIST_SELECT} FROM facilities "
        f"WHERE state IN ({_state_placeholders(states)}) "
        f"  AND tag_active_snc = 1 "
        f"ORDER BY lead_score DESC "
        f"LIMIT ?"
    )
    rows = conn.execute(sql, [*states, limit]).fetchall()
    return _rows_to_dicts(rows, _LEAD_TAG_COLS)


def _query_region_summary(conn, states: list[str]) -> dict:
    """High-level slice: by program, by outreach_posture, top tag fires.
    Keeps the LLM oriented when it first asks about a region."""
    out: dict = {"states": states}

    by_program = conn.execute(
        f"SELECT program, COUNT(*) AS n, "
        f"       SUM(CASE WHEN lead_score >= 50 THEN 1 ELSE 0 END) AS high_value "
        f"FROM facilities WHERE state IN ({_state_placeholders(states)}) "
        f"GROUP BY program",
        states,
    ).fetchall()
    out["by_program"] = [dict(r) for r in by_program]

    by_posture = conn.execute(
        f"SELECT outreach_posture, COUNT(*) AS n FROM facilities "
        f"WHERE state IN ({_state_placeholders(states)}) "
        f"GROUP BY outreach_posture ORDER BY n DESC",
        states,
    ).fetchall()
    out["by_outreach_posture"] = [dict(r) for r in by_posture]

    out["tag_counts"] = {}
    for tag in _LEAD_TAG_COLS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM facilities "
            f"WHERE state IN ({_state_placeholders(states)}) "
            f"  AND {tag} = 1",
            states,
        ).fetchone()[0]
        if n:
            out["tag_counts"][tag] = n
    return out


def _query_lead_detail(conn, registry_id: str, program: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM facilities WHERE registry_id = ? AND program = ?",
        (registry_id, program),
    ).fetchone()
    if row is None:
        return None
    d = {k: row[k] for k in row.keys() if row[k] is not None}
    for k in list(d.keys()):
        if k.startswith("tag_"):
            d[k] = bool(d[k])
    return d


def _query_violation_events(conn, registry_id: str, program: str,
                            limit: int) -> list[dict]:
    sql = (
        "SELECT violation_id, violation_category, violation_description, "
        "       contaminant, rule_family, parameter, limit_value, dmr_value, "
        "       exceedance_pct, period_begin, period_end, resolved_date, status "
        "FROM violations "
        "WHERE registry_id = ? AND program = ? "
        # NULLs last so real-dated rows come first
        "ORDER BY period_end IS NULL, period_end DESC "
        "LIMIT ?"
    )
    rows = conn.execute(sql, (registry_id, program, limit)).fetchall()
    return [
        {k: r[k] for k in r.keys() if r[k] is not None}
        for r in rows
    ]


# ----------------------------------------------------------- tool layer
#
# Tools take a `ctx` dict carrying all the runtime config the script
# threads through (db paths, candidate cap, force-rebrief flag, plus a
# `candidates_tracker` dict the briefing_candidates tool writes into so
# the script can record which leads were shown to the LLM). This single-
# signature shape keeps the dispatcher simple and adds new context
# without rewriting every tool.

def _resolve_region(region: str) -> list[str]:
    try:
        return regions.states_for(region)
    except KeyError:
        raise ValueError(
            f"unknown region {region!r}. Valid: {regions.all_region_names()}"
        )


def tool_list_regions(_ctx: dict, _args: dict) -> dict:
    return {"regions": regions.all_region_names()}


def tool_region_summary(ctx: dict, args: dict) -> dict:
    states = _resolve_region(args["region"])
    with _open_db(ctx["db_path"]) as conn:
        return _query_region_summary(conn, states)


def tool_top_leads(ctx: dict, args: dict) -> dict:
    states = _resolve_region(args["region"])
    limit = min(int(args.get("limit", 20)), 100)
    min_score = int(args.get("min_score", 50))
    with _open_db(ctx["db_path"]) as conn:
        return {"leads": _query_top_leads(conn, states, limit, min_score)}


def tool_newly_seen(ctx: dict, args: dict) -> dict:
    states = _resolve_region(args["region"])
    days = min(max(int(args.get("since_days", 14)), 1), 90)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    limit = min(int(args.get("limit", 20)), 100)
    min_score = int(args.get("min_score", 30))
    with _open_db(ctx["db_path"]) as conn:
        return {
            "since_days": days,
            "since_iso": cutoff,
            "leads": _query_newly_seen(conn, states, cutoff, limit, min_score),
        }


def tool_active_snc(ctx: dict, args: dict) -> dict:
    states = _resolve_region(args["region"])
    limit = min(int(args.get("limit", 20)), 100)
    with _open_db(ctx["db_path"]) as conn:
        return {"leads": _query_active_snc(conn, states, limit)}


def tool_lead_detail(ctx: dict, args: dict) -> dict:
    with _open_db(ctx["db_path"]) as conn:
        row = _query_lead_detail(conn, args["registry_id"], args["program"])
    if row is None:
        return {"error": "no facility with that (registry_id, program)"}
    return row


def tool_violation_events(ctx: dict, args: dict) -> dict:
    limit = min(int(args.get("limit", 10)), 50)
    with _open_db(ctx["db_path"]) as conn:
        events = _query_violation_events(
            conn, args["registry_id"], args["program"], limit
        )
    return {"events": events}


def tool_briefing_candidates(ctx: dict, args: dict) -> dict:
    """Return leads that warrant a fresh briefing in this region.

    Cap on the returned set is enforced by `ctx['leads_per_region']`;
    the LLM cannot exceed it even if it asks for more. Each returned
    row carries a `briefing_status` (never_briefed / score_changed /
    new_activity) so the prose can frame the lead correctly.

    Side-effect: the returned candidate set is recorded into
    `ctx['candidates_tracker'][region]` so the script can mark these
    leads as featured after the briefing completes (when
    `--mark-briefed` is set).
    """
    region = args["region"]
    states = _resolve_region(region)
    cap = int(ctx["leads_per_region"])
    requested = int(args.get("limit", cap))
    limit = min(requested, cap)
    min_score = int(args.get("min_score", 50))

    candidates = state.candidates_for_states(
        snapshot_path=ctx["db_path"],
        state_path=ctx["state_path"],
        states=states,
        limit=limit,
        min_score=min_score,
        force_rebrief=ctx.get("force_rebrief", False),
    )

    tracker = ctx.get("candidates_tracker")
    if tracker is not None:
        # Stash minimal info needed to mark these later.
        tracker.setdefault(region, [])
        existing_keys = {
            (c["registry_id"], c["program"]) for c in tracker[region]
        }
        for c in candidates:
            key = (c.get("registry_id"), c.get("program"))
            if key[0] and key[1] and key not in existing_keys:
                tracker[region].append({
                    "registry_id": c["registry_id"],
                    "program": c["program"],
                    "lead_score": c.get("lead_score"),
                    "last_seen": c.get("last_seen"),
                })
                existing_keys.add(key)

    return {
        "region": region,
        "leads_per_region_cap": cap,
        "candidates": candidates,
    }


TOOL_HANDLERS = {
    "list_regions": tool_list_regions,
    "region_summary": tool_region_summary,
    "briefing_candidates": tool_briefing_candidates,
    "top_leads": tool_top_leads,
    "newly_seen": tool_newly_seen,
    "active_snc": tool_active_snc,
    "lead_detail": tool_lead_detail,
    "violation_events": tool_violation_events,
}


# ----------------------------------------------------------- tool schemas

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_regions",
            "description": (
                "List the names of all sales regions defined for this "
                "tool. Every other tool that takes a `region` argument "
                "expects one of these names verbatim."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "region_summary",
            "description": (
                "High-level shape of a region: lead counts split by "
                "program (CWA/SDWA) and by outreach posture, plus the "
                "set of signal-tag fires across the region. Use this "
                "first to orient before pulling lead lists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                },
                "required": ["region"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "briefing_candidates",
            "description": (
                "Primary source of briefing material. Returns leads in "
                "the region that warrant a fresh briefing — either "
                "never-briefed, scored changed by more than 5 since last "
                "briefing, or fresh EPA activity since last briefing. "
                "The cap on returned leads is enforced by the script "
                "(typically 10 per region); you cannot exceed it. "
                "Each row carries `briefing_status` so you know whether "
                "to frame as a first-time feature or an update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "limit": {"type": "integer",
                              "description": "Requested cap — actual "
                              "ceiling is the script's --leads-per-region "
                              "(default 10)."},
                    "min_score": {"type": "integer",
                                  "description": "Minimum lead_score "
                                  "(default 50)."},
                },
                "required": ["region"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_leads",
            "description": (
                "Top-scored leads in a region, ignoring briefing history. "
                "Use sparingly — only when you need broader context that "
                "`briefing_candidates` didn't surface (e.g. to mention "
                "an inventory tier the candidates don't reflect). The "
                "main feature list should come from `briefing_candidates`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "limit": {"type": "integer",
                              "description": "Max rows (1-100, default 20)."},
                    "min_score": {"type": "integer",
                                  "description": "Minimum lead_score "
                                  "(default 50)."},
                },
                "required": ["region"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "newly_seen",
            "description": (
                "Facilities first observed in the snapshot DB in the "
                "last N days. Use this to surface fresh signal — leads "
                "that appeared since the prior weekly run. Mostly "
                "supplementary to briefing_candidates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "since_days": {"type": "integer",
                                   "description": "Window in days "
                                   "(1-90, default 14)."},
                    "limit": {"type": "integer",
                              "description": "Max rows (1-100, default 20)."},
                    "min_score": {"type": "integer",
                                  "description": "Minimum lead_score "
                                  "(default 30)."},
                },
                "required": ["region"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "active_snc",
            "description": (
                "Currently-flagged Significant Non-Compliers in a "
                "region. SNC is EPA's red-flag designation — the "
                "strongest single signal in the system. Useful when "
                "you want to call attention to SNCs the candidate set "
                "didn't include."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "limit": {"type": "integer",
                              "description": "Max rows (1-100, default 20)."},
                },
                "required": ["region"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lead_detail",
            "description": (
                "Full row for one facility, including compliance "
                "snapshot, pre-violation signals, exceedance summary, "
                "and the ECHO URL. Use sparingly — only for leads you "
                "intend to feature in the briefing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "registry_id": {"type": "string"},
                    "program": {"type": "string", "enum": ["CWA", "SDWA"]},
                },
                "required": ["registry_id", "program"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "violation_events",
            "description": (
                "Recent violation events for one facility. CWA events "
                "carry per-DMR parameter + exceedance %; SDWA events "
                "carry violation category + status. Use when you want "
                "to call out specific exceedances in the briefing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "registry_id": {"type": "string"},
                    "program": {"type": "string", "enum": ["CWA", "SDWA"]},
                    "limit": {"type": "integer",
                              "description": "Max events (1-50, default 10)."},
                },
                "required": ["registry_id", "program"],
            },
        },
    },
]


def dispatch(name: str, args: dict, ctx: dict) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return handler(ctx, args)
    except (KeyError, ValueError, TypeError, sqlite3.Error) as e:
        # Return errors as data so the model can try a different
        # argument rather than the loop crashing on a bad tool call.
        return {"error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------- prompts

SYSTEM_PROMPT = """\
You are a sales analyst at ChemTreat, a water-treatment chemicals
company. You draft weekly briefings for regional sales leaders who
already understand the EPA data model.

The briefing should:

1. Open with a one-paragraph summary of the region this week.

2. Feature the leads returned by `briefing_candidates` — that tool
   enforces the volume cap and only returns leads worth re-surfacing.
   Each candidate carries a `briefing_status`:
     * never_briefed: first time on a briefing — frame as a fresh find
     * score_changed: re-surfacing with a score shift — cite the
       delta using `prior_lead_score` ("score jumped 142 → 168")
     * new_activity: re-surfacing because the bulk run touched it
       since last briefing — usually means new event data or refreshed
       signals; mention what's new

3. For EACH featured lead, write a short detail block — not a single
   bullet, not a paragraph. Aim for ~4-6 sentences that give the rep
   enough to walk into the conversation prepared. Pull from the row
   data directly:

     **Company name (city, state, score [Δ if score_changed])** — *industry*
       - **What's wrong:** specific compliance picture. Cite the
         numbers: "11 quarters in non-compliance, 3 formal actions
         over 5 years, $35K penalty on 2025-08-14." Always cite the
         `last_penalty_date` when you mention a penalty amount — date
         matters as much as size for whether the situation is fresh.
         Don't say "multiple issues" when you have counts.
       - **Recent activity:** when `recent_events` is on the row, cite
         the 2-3 most recent by date: "2026-03 BOD exceedance 50%
         over limit, 2026-01 BOD 48% over." Specific event dates
         ground the briefing in reality vs. abstract counts. If only
         one event date is available, cite the one.
       - **The angle:** why this is a *ChemTreat* lead specifically.
         The authoritative field is `exceeded_treatable_parameters_text`
         — those are parameters they're currently exceeding AND that
         ChemTreat treats. Cite them ("currently exceeding BOD by 50%,
         and their permit explicitly covers BOD").

         **Worst-vs-treatable check:** if `top_exceeded_parameter` is
         NOT one of the values listed in
         `exceeded_treatable_parameters_text`, surface BOTH explicitly:
         "Their worst single exceedance was X (e.g. Whole Effluent
         Toxicity, which our chemistry doesn't directly address), but
         the ChemTreat angle is Y — they're also exceeding ammonia by
         180% and their permit covers ammonia." Don't lead with the
         worst exceedance if it's not in our chemistry — it sets up
         the wrong conversation.

         If `matching_impaired_parameters` is set, that's the strongest
         angle ("their monitored BOD is documented as a cause of the
         downstream waterbody's impairment — regulator attention is
         already there, next permit renewal will tighten"). If only
         `permitted_parameters_text` is set and no exceedance yet,
         frame as account-research ("permit covers phosphorus and
         ammonia — at next renewal these limits tighten").
       - **Verify on ECHO:** include the `echo_url` so the rep can
         spot-check before outreach.

   Use the `industry` field when present — "Fluid Milk Manufacturing"
   reads as a real lead; "NAICS 311511" reads as a database row. If
   `industry` is missing, leave the slot empty rather than printing
   the bare code.

   Skip any field that's empty — don't write "No data available";
   just omit it. The reader will see what's there is what matters.

4. Group thoughtfully — by score tier, by industry/NAICS, or by signal
   class (active SNC vs. pre-violation vs. recent exceedance). Pick
   the grouping that produces the cleanest story; don't force all
   three.

5. Note any "verify-first" cases (outreach_posture = verify_first or
   historical) so the rep doesn't cold-call about resolved issues.

6. End with 1-3 concrete next steps for the rep.

If `briefing_candidates` returns 0 candidates, say so plainly — the
region is in a steady state and there's nothing fresh to brief. Don't
fall back to `top_leads` just to fill space.

Be terse where you can — sales leaders are busy. But per-lead, surface
the specifics. A vague briefing is worse than no briefing because the
rep then has to dig the same information out of ECHO themselves. The
whole point is to save them that step.

Markdown formatting. No preamble like "Here is your briefing" — start
with the heading. Don't invent data; only cite numbers you've seen via
a tool call. If you want detail not in the candidate row (e.g. specific
per-DMR exceedance dates or SDWA violation status), call
`violation_events` for that lead.

EPA data lags 30-90 days. If you reference a violation, assume it may
be older than it looks and frame accordingly.
"""


def build_user_prompt(region: str, today: str) -> str:
    lead = regions.lead_for(region)
    return (
        f"Draft this week's sales briefing for the {region} region. "
        f"Today is {today}. The audience is {lead['name']}.\n\n"
        f"Workflow: call `region_summary` first to orient, then "
        f"`briefing_candidates` to get the feature list (volume-capped "
        f"to what's worth surfacing this run), then `lead_detail` or "
        f"`violation_events` only for standouts you want to detail. "
        f"Skip `top_leads` unless the candidate set is short and you "
        f"need broader context."
    )


# ----------------------------------------------------------- LLM loop

def run_one_region(client, model: str, region: str,
                   ctx: dict) -> str:
    """Run the tool-use loop for one region. Returns the final briefing
    text the model emitted. `model` is either an Azure deployment
    name or a direct-OpenAI model id — `client.chat.completions.create`
    takes the same parameter name either way. `ctx` is the runtime
    config dict — see `tool_briefing_candidates` for what it contains."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(region, today)},
    ]

    for turn in range(MAX_TURNS):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=0.3,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg.model_dump(exclude_unset=True, exclude_none=True))
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = dispatch(tc.function.name, args, ctx)
            log.info("[%s] tool=%s args=%s -> %d-key result",
                     region, tc.function.name, args,
                     len(result) if isinstance(result, dict) else -1)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    raise RuntimeError(
        f"[{region}] exceeded {MAX_TURNS} tool-use turns without a "
        f"final answer. Tighten the prompt or raise MAX_TURNS."
    )


# ----------------------------------------------------------- email

def render_email(region: str, body: str, today: str) -> EmailMessage:
    lead = regions.lead_for(region)
    msg = EmailMessage()
    msg["Subject"] = f"ChemTreat sales briefing — {region} — {today}"
    msg["To"] = f"{lead['name']} <{lead['email']}>"
    msg["From"] = os.environ.get("BRIEFINGS_FROM_ADDRESS", "noreply@example.com")
    msg.set_content(body)
    return msg


def send_email(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    log.info("Sent briefing to %s", msg["To"])


# ----------------------------------------------------------- CLI / main

def _load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny `.env` loader. Reads KEY=VALUE lines, skips blanks and
    comments, sets env vars that aren't already set. Shell-exported
    vars always win — `.env` only fills in missing ones.

    Quoted values get their surrounding quotes stripped so a key like
    `OPENAI_API_KEY="sk-..."` works the same as the unquoted form.
    Intentionally minimal: no variable expansion, no multi-line
    values, no escape sequences. If you need any of that, add
    python-dotenv to deps and swap this out.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _build_client(direct_openai: bool):
    """Construct an OpenAI-compatible chat client.

    `direct_openai=True` routes to api.openai.com using a personal
    `OPENAI_API_KEY` — the quick-test path so you can validate the
    flow without an Azure deployment in place. Returns whatever the
    `openai` SDK gives back; chat.completions.create with tools is
    shape-identical between the two clients, so nothing downstream
    has to care which one fired.

    `direct_openai=False` is the production path against the org's
    Azure OpenAI deployment.
    """
    try:
        from openai import AzureOpenAI, OpenAI
    except ImportError:
        sys.exit(
            "openai package not installed. Add it to the workspace venv:\n"
            "  cd ~/PycharmProjects/Work && uv add openai"
        )

    if direct_openai:
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit(
                "--openai-direct requires OPENAI_API_KEY in env or .env. "
                "See README.md 'Quick test with a personal OpenAI key'."
            )
        kwargs = {"api_key": os.environ["OPENAI_API_KEY"]}
        if os.environ.get("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        return OpenAI(**kwargs)

    missing = [k for k in
               ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT")
               if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env: {', '.join(missing)}. See README.md.")
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


def _resolve_model(direct_openai: bool) -> str:
    """Which model to pass to chat.completions.create.

    Azure uses the deployment name (set during provisioning); direct
    OpenAI uses a model id like `gpt-4o-mini`. Default the direct path
    to `gpt-4o-mini` — capable enough for this task and cheap enough
    to iterate on tone freely.
    """
    if direct_openai:
        return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return os.environ["AZURE_OPENAI_DEPLOYMENT"]


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate regional ChemTreat sales briefings via "
                    "Azure OpenAI with narrow snapshot.sqlite tools."
    )
    p.add_argument("--db", default="snapshot.sqlite",
                   help="Path to snapshot.sqlite (default: snapshot.sqlite, "
                        "i.e. the current working directory — typically "
                        "EPA/ when invoked per the README)")
    p.add_argument("--state-db", default="briefings_state.sqlite",
                   help="Path to briefings_state.sqlite — tracks which "
                        "leads have been featured (default: "
                        "briefings_state.sqlite, alongside snapshot.sqlite)")
    p.add_argument("--regions", default=None,
                   help="Comma-separated region names (default: all)")
    p.add_argument("--leads-per-region", type=int, default=10,
                   help="Hard cap on candidates per region per run "
                        "(default 10). The LLM cannot exceed this.")
    p.add_argument("--send", action="store_true",
                   help="Actually send email. Default is --dry-run "
                        "(print to stdout).")
    p.add_argument("--mark-briefed", action="store_true",
                   help="After each region's briefing, mark the "
                        "candidate leads as featured. Without this "
                        "flag, no state is written — useful for "
                        "iterating on tone. Independent of --send "
                        "so you can mark dry-runs or send without "
                        "marking, whichever fits your workflow.")
    p.add_argument("--force-rebrief", action="store_true",
                   help="Ignore the candidate filter — return top "
                        "leads by score regardless of briefing "
                        "history. For testing the gating logic.")
    p.add_argument("--openai-direct", action="store_true",
                   help="Use api.openai.com directly with OPENAI_API_KEY "
                        "instead of Azure. Quick-test path for "
                        "validating the flow before Azure provisioning. "
                        "Reads OPENAI_API_KEY (required) and "
                        "OPENAI_MODEL (default gpt-4o-mini) from env "
                        "or .env. See README.md.")
    p.add_argument("-v", "--verbose", action="store_true")

    # Load .env BEFORE arg parsing's validation chain — keys can come
    # from the file too. argparse itself doesn't read env; this just
    # populates os.environ so the downstream credential checks see it.
    _load_dotenv()

    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    state_path = Path(args.state_db).expanduser().resolve()
    # init creates the file + tables if missing. Idempotent.
    state.init_state_db(state_path)
    log.info("Briefings state DB: %s", state_path)

    if args.regions:
        wanted = [r.strip() for r in args.regions.split(",")]
        for r in wanted:
            if r not in regions.REGIONS:
                sys.exit(f"Unknown region {r!r}. "
                         f"Valid: {regions.all_region_names()}")
    else:
        wanted = regions.all_region_names()

    if args.send:
        missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
                   if not os.environ.get(k)]
        if missing:
            sys.exit(f"--send requires env: {', '.join(missing)}")

    client = _build_client(args.openai_direct)
    model = _resolve_model(args.openai_direct)
    log.info("Using %s model: %s",
             "OpenAI (direct)" if args.openai_direct else "Azure OpenAI",
             model)
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # candidates_tracker collects the candidate set the LLM saw, per
    # region. The mark step at the end of each region reads from this
    # to know what to write.
    candidates_tracker: dict[str, list[dict]] = {}

    for region in wanted:
        log.info("Drafting briefing for %s...", region)
        ctx = {
            "db_path": db_path,
            "state_path": state_path,
            "leads_per_region": args.leads_per_region,
            "force_rebrief": args.force_rebrief,
            "candidates_tracker": candidates_tracker,
        }
        body = run_one_region(client, model, region, ctx)
        msg = render_email(region, body, today)

        if args.send:
            send_email(msg)
        else:
            print("=" * 72)
            print(f"DRY RUN — {region}")
            print("=" * 72)
            print(f"To:      {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            print()
            print(body)
            print()

        if args.mark_briefed:
            featured = candidates_tracker.get(region, [])
            if featured:
                mode = "send" if args.send else "dry_run"
                run_id = state.record_briefing_run(
                    state_path, region, mode, featured)
                log.info("Marked %d leads as featured for %s "
                         "(briefing_run_id=%d)", len(featured), region, run_id)
            else:
                log.info("No candidates surfaced for %s — nothing to mark",
                         region)


if __name__ == "__main__":
    main()
