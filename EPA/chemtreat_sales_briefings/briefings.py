"""Regional sales briefings: query snapshot.sqlite via narrow tools,
let Azure OpenAI draft a briefing per region, optionally email it.

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

Run with `--dry-run` (default) to print to stdout. `--send` enables the
SMTP path; required env vars are validated before any LLM call.

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

from . import regions


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

    # Top-signal tag fires across the region. Each tag has its own count
    # rather than a SUM over a CASE chain so we surface them all in one
    # query without per-tag SQL repetition.
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
# Each tool is a thin adapter: validate arguments, resolve `region` to
# its state list, call the matching `_query_*` function, return the
# result. Error paths return a `{"error": str}` dict so the LLM can
# react instead of the loop crashing.

def _resolve_region(region: str) -> list[str]:
    try:
        return regions.states_for(region)
    except KeyError:
        raise ValueError(
            f"unknown region {region!r}. Valid: {regions.all_region_names()}"
        )


def tool_list_regions(_db, _args: dict) -> dict:
    return {"regions": regions.all_region_names()}


def tool_region_summary(db_path: Path, args: dict) -> dict:
    states = _resolve_region(args["region"])
    with _open_db(db_path) as conn:
        return _query_region_summary(conn, states)


def tool_top_leads(db_path: Path, args: dict) -> dict:
    states = _resolve_region(args["region"])
    limit = min(int(args.get("limit", 20)), 100)
    min_score = int(args.get("min_score", 50))
    with _open_db(db_path) as conn:
        return {"leads": _query_top_leads(conn, states, limit, min_score)}


def tool_newly_seen(db_path: Path, args: dict) -> dict:
    states = _resolve_region(args["region"])
    days = min(max(int(args.get("since_days", 14)), 1), 90)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    limit = min(int(args.get("limit", 20)), 100)
    min_score = int(args.get("min_score", 30))
    with _open_db(db_path) as conn:
        return {
            "since_days": days,
            "since_iso": cutoff,
            "leads": _query_newly_seen(conn, states, cutoff, limit, min_score),
        }


def tool_active_snc(db_path: Path, args: dict) -> dict:
    states = _resolve_region(args["region"])
    limit = min(int(args.get("limit", 20)), 100)
    with _open_db(db_path) as conn:
        return {"leads": _query_active_snc(conn, states, limit)}


def tool_lead_detail(db_path: Path, args: dict) -> dict:
    with _open_db(db_path) as conn:
        row = _query_lead_detail(conn, args["registry_id"], args["program"])
    if row is None:
        return {"error": "no facility with that (registry_id, program)"}
    return row


def tool_violation_events(db_path: Path, args: dict) -> dict:
    limit = min(int(args.get("limit", 10)), 50)
    with _open_db(db_path) as conn:
        events = _query_violation_events(
            conn, args["registry_id"], args["program"], limit
        )
    return {"events": events}


TOOL_HANDLERS = {
    "list_regions": tool_list_regions,
    "region_summary": tool_region_summary,
    "top_leads": tool_top_leads,
    "newly_seen": tool_newly_seen,
    "active_snc": tool_active_snc,
    "lead_detail": tool_lead_detail,
    "violation_events": tool_violation_events,
}


# ----------------------------------------------------------- tool schemas
#
# OpenAI function-calling shape. Descriptions matter — the model uses
# them to decide which tool fits the current need. Be specific about
# what each tool is for and what shape it returns.

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
            "name": "top_leads",
            "description": (
                "Top-scored leads in a region. Returns a list of "
                "facility rows with score, reasons, outreach posture, "
                "and the relevant signal tags. Use this as the main "
                "source of briefing material."
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
                "that appeared since the prior weekly run."
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
                "strongest single signal in the system."
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


def dispatch(name: str, args: dict, db_path: Path) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return handler(db_path, args)
    except (KeyError, ValueError, TypeError, sqlite3.Error) as e:
        # Return errors as data so the model can try a different
        # argument rather than the loop crashing on a bad tool call.
        return {"error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------- prompts

SYSTEM_PROMPT = """\
You are a sales analyst at ChemTreat, a water-treatment chemicals
company. You draft concise weekly briefings for regional sales leaders
who already understand the EPA data model.

The briefing should:
  - Open with a one-paragraph summary of the region this week.
  - Highlight 5-10 leads worth the rep's attention, with a one-line
    rationale each. Lead with `company (city, state, score)`.
  - Group thoughtfully — by score tier, by industry, or by signal class
    (active SNC vs. pre-violation vs. recent exceedance). Pick the
    grouping that produces the cleanest story; don't force all three.
  - Note any "verify-first" cases (outreach_posture = verify_first or
    historical) so the rep doesn't cold-call about resolved issues.
  - End with 1-3 concrete next steps for the rep.

Be terse. Sales leaders are busy. Markdown formatting. No preamble like
"Here is your briefing" — start with the heading. Don't invent data;
only cite numbers you've seen via a tool call.

EPA data lags 30-90 days. If you reference a violation, assume it may
be older than it looks and frame accordingly.
"""


def build_user_prompt(region: str, today: str) -> str:
    lead = regions.lead_for(region)
    return (
        f"Draft this week's sales briefing for the {region} region. "
        f"Today is {today}. The audience is {lead['name']}.\n\n"
        f"Start by calling `region_summary` to get oriented. Then pull "
        f"top leads, fresh activity (newly_seen), and active SNC. Use "
        f"`lead_detail` or `violation_events` for any standout you plan "
        f"to feature."
    )


# ----------------------------------------------------------- LLM loop

def run_one_region(client, deployment: str, region: str,
                   db_path: Path) -> str:
    """Run the tool-use loop for one region. Returns the final briefing
    text the model emitted."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(region, today)},
    ]

    for turn in range(MAX_TURNS):
        response = client.chat.completions.create(
            model=deployment,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=0.3,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        # Append the assistant turn (with tool_calls) verbatim, then
        # one tool response per call, then loop. model_dump turns the
        # SDK object into the dict shape the next request expects.
        messages.append(msg.model_dump(exclude_unset=True, exclude_none=True))
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = dispatch(tc.function.name, args, db_path)
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

def _build_client():
    """Construct the Azure OpenAI client. Fails loudly if env is unset
    so a missing credential never silently becomes a no-op."""
    try:
        from openai import AzureOpenAI
    except ImportError:
        sys.exit(
            "openai package not installed. Add it to the workspace venv:\n"
            "  cd ~/PycharmProjects/Work && uv add openai"
        )
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


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate regional ChemTreat sales briefings via "
                    "Azure OpenAI with narrow snapshot.sqlite tools."
    )
    p.add_argument("--db", default="../snapshot.sqlite",
                   help="Path to snapshot.sqlite (default: ../snapshot.sqlite)")
    p.add_argument("--regions", default=None,
                   help="Comma-separated region names (default: all)")
    p.add_argument("--send", action="store_true",
                   help="Actually send email. Default is --dry-run "
                        "(print to stdout).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    if args.regions:
        wanted = [r.strip() for r in args.regions.split(",")]
        for r in wanted:
            if r not in regions.REGIONS:
                sys.exit(f"Unknown region {r!r}. "
                         f"Valid: {regions.all_region_names()}")
    else:
        wanted = regions.all_region_names()

    if args.send:
        # Validate SMTP env upfront — surface missing creds before any
        # tokens are spent on LLM calls.
        missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
                   if not os.environ.get(k)]
        if missing:
            sys.exit(f"--send requires env: {', '.join(missing)}")

    client = _build_client()
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for region in wanted:
        log.info("Drafting briefing for %s...", region)
        body = run_one_region(client, deployment, region, db_path)
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


if __name__ == "__main__":
    main()
