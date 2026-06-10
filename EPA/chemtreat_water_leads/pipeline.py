"""
pipeline.py
===========
The end-to-end run. Glues the API client, scorer, and snapshot together.

Flow:

    1. Pull current violators per (state, NAICS) combination
    2. Score each facility (with reasons)
    3. For high-scoring facilities, drill into individual violation events
       (DMR exceedances for CWA, SDWA violations from the DFR)
    4. Diff against last run's snapshot (SQLite)
    5. Write three CSVs:
         a. all_leads.csv        - full ranked inventory
         b. new_today.csv        - just what changed since last run
         c. violation_events.csv - the underlying individual events
    6. Update the snapshot DB

Usage:
    python -m chemtreat_water_leads.pipeline --states TX,LA,OH --out ./out
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import _health, echo_client, scoring, snapshot

log = logging.getLogger("chemtreat")

# Industries ChemTreat typically serves. Edit freely - this is a marketing
# decision, not a technical one.
TARGET_NAICS = [
    "2211", "311", "312", "322", "324", "325", "327",
    "331", "332", "336", "622", "2111", "212",
]

# We only drill into violation *events* for high-scoring facilities,
# because that endpoint is per-permit (slow). Cutoff is tunable.
EVENT_DRILLDOWN_MIN_SCORE = 50

# How far back to look for individual DMR violations.
LOOKBACK_DAYS = 365


# ---------------------------------------------------- DRILL-DOWN BACKOFF
#
# Per-row drill-down state (recorded on `facilities` via the columns
# `last_drilldown_attempt_at`, `last_drilldown_outcome`,
# `drilldown_failure_streak`, `next_drilldown_eligible_at`) drives the
# hands-off rerun loop. The Snowflake eligibility view filters
# `WHERE next_drilldown_eligible_at <= CURRENT_TIMESTAMP()`; the pipeline
# writes that timestamp at the end of each drill attempt based on this
# policy.
#
# `DRILLDOWN_BACKOFF` covers the non-escalating outcomes — both come
# back from EPA with a definite answer ("here are events" / "no events
# on file"), so the right retry cadence is a function of how often EPA
# itself refreshes the underlying data:
#   * with_events: 7d (matches EPA's weekly bulk refresh — drilling more
#     often returns the same data)
#   * no_data: 30d (EPA confirmed no events on file; burning quota to
#     re-confirm doesn't earn anything until something *new* lands)
#
# `lookup_failed` is structurally different — it means EPA's API didn't
# answer at all (HTTP 429 / connection drop / bot-block). The right
# retry cadence depends on whether this is the FIRST throttle hit (likely
# transient, clear in hours) or part of a sustained block (verified
# 2026-06-10: EPA returns no Retry-After header and the block persists
# across endpoints for >24h). So lookup_failed lives in its own
# streak-tiered table — see `_lookup_failed_backoff` below.
DRILLDOWN_BACKOFF: dict[str, timedelta] = {
    "with_events": timedelta(days=7),
    "no_data":     timedelta(days=30),
}

# Recognised outcome values. `_record_drilldown_outcome` checks against
# this set rather than DRILLDOWN_BACKOFF directly, because lookup_failed
# is no longer a key in that dict.
_VALID_OUTCOMES: frozenset[str] = frozenset(
    DRILLDOWN_BACKOFF.keys() | {"lookup_failed"}
)

# Streak-tiered backoff for `lookup_failed`. Highest-threshold-first list;
# `_lookup_failed_backoff` iterates and returns the first match against
# the *new* streak (i.e. after this attempt's increment).
#
# The tiers encode three operational regimes:
#   * streak 1-2 → 6h:  a single transient throttle. EPA's bot-block and
#                       DFR thin-response throttles both clear within
#                       minutes; 6h is comfortable margin and dovetails
#                       with the daily rerun cadence.
#   * streak 3-4 → 24h: sustained throttle. Either we tripped EPA's
#                       rolling rate limit hard, or peak-hours load means
#                       short-term retry is wasted. 24h skips a full
#                       business-day window.
#   * streak 5+  → 7d:  persistent block. At this point we're in the
#                       weekly-bulk-refresh regime — there's almost
#                       certainly nothing new EPA could give us until
#                       their own refresh cycle anyway. Bonus: lines
#                       failed leads up with the bulk cadence so they
#                       retry alongside fresh discovery rather than
#                       fighting it.
LOOKUP_FAILED_BACKOFF_TIERS: tuple[tuple[int, timedelta], ...] = (
    (5, timedelta(days=7)),
    (3, timedelta(days=1)),
    (1, timedelta(hours=6)),
)


def _lookup_failed_backoff(new_streak: int) -> timedelta:
    """Pick the backoff for a `lookup_failed` outcome based on its
    *post-increment* streak. Highest matching tier wins."""
    for threshold, backoff in LOOKUP_FAILED_BACKOFF_TIERS:
        if new_streak >= threshold:
            return backoff
    # Unreachable in practice (the (1, 6h) tier matches any streak >= 1
    # and _record_drilldown_outcome only calls this with streak >= 1),
    # but defaulting to the 6h floor keeps a hypothetical streak=0 caller
    # consistent rather than crashing.
    return timedelta(hours=6)


def _record_drilldown_outcome(lead: dict, outcome: str,
                               prior_streaks: dict[tuple[str, str], int],
                               now: datetime) -> None:
    """Annotate `lead` with the per-row drill-down state for this attempt.

    Sets four columns the snapshot upsert reads via `.get()`:
    `last_drilldown_attempt_at`, `last_drilldown_outcome`,
    `drilldown_failure_streak`, `next_drilldown_eligible_at`. The
    fifth column (`last_drilldown_run_id`) is backfilled by
    `snapshot.diff_and_upsert_facilities` from the run_id it already
    has — keeps record_run atomic with the upsert block (no early
    DB open required just to mint a run_id for drilling).

    Called once per drill attempt by `_drill_cwa` / `_drill_sdwa`;
    a later attempt in the same run (e.g. the second pass after EPA
    throttle clears) cleanly overwrites the earlier fields, mirroring
    the existing `failed_keys` discard-on-success semantics.

    Streak math:
      * 'lookup_failed' -> prior_streak + 1, backoff via the streak-tiered
        `LOOKUP_FAILED_BACKOFF_TIERS` (6h → 24h → 7d as failures pile up)
      * 'with_events' / 'no_data' -> 0  (reset on any non-failure); backoff
        from the flat `DRILLDOWN_BACKOFF` dict (7d / 30d)
    """
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"unknown drill-down outcome: {outcome!r}")
    key = (lead.get("registry_id"), lead.get("program"))
    prior = prior_streaks.get(key, 0)
    if outcome == "lookup_failed":
        streak = prior + 1
        backoff = _lookup_failed_backoff(streak)
    else:
        streak = 0
        backoff = DRILLDOWN_BACKOFF[outcome]
    lead["last_drilldown_attempt_at"] = now.isoformat(timespec="seconds")
    lead["last_drilldown_outcome"] = outcome
    lead["drilldown_failure_streak"] = streak
    lead["next_drilldown_eligible_at"] = (
        (now + backoff).isoformat(timespec="seconds")
    )


# ---------------------------------------------------------- LAG WARNINGS
#
# EPA's water data is NOT real-time. The two programs lag for different
# reasons and by different amounts. We surface this in four places:
#   1. Banner printed at the start of every run
#   2. data_lag_note column on each event row
#   3. READ_ME_FIRST.txt written next to the CSVs each run
#   4. Final summary banner at end of run
#
# The bigger lag (SDWA) is the one to over-communicate. A rep cold-calling
# about a SDWA violation that was actually resolved 4 months ago is bad.

SDWA_LAG_NOTE = (
    "SDWA reporting lag ~90 days. Per EPA: violation and enforcement data "
    "are reported quarterly to the federal system no later than the quarter "
    "FOLLOWING the quarter in which events occur. A violation listed here "
    "may have already been resolved on the ground. Verify status before "
    "outreach."
)

CWA_LAG_NOTE = (
    "CWA DMR lag ~30-45 days. Monthly Discharge Monitoring Reports are "
    "filed after the monitoring period closes. Very recent activity (last "
    "30 days) is incomplete."
)

LAG_BANNER = """
=====================================================================
  IMPORTANT: EPA DATA IS NOT REAL-TIME
  ---------------------------------------------------------------
  SDWA (drinking water): ~90-day reporting lag (quarterly cadence)
  CWA  (wastewater):     ~30-45 day lag (monthly DMR cadence)

  Treat this as a prioritization signal, not breaking news. A
  'newly seen' violation in today's diff may be months old in
  reality - especially for SDWA.
=====================================================================
"""


# ---------------------------------------------------------------- helpers

def _flatten_facility(raw: dict, program: str) -> dict:
    """Normalize ECHO's CWA/SDWA records into one schema.

    Field-name reality check:
      - CWA responses from get_qid use CWP*-prefixed fields (CWPName,
        CWPCity, CWPState, ...) because the data is permit-centric
        ("Clean Water Permit").
      - SDWA responses use PWS*-prefixed fields ("Public Water System").
      - Some endpoints also include Fac* generic fields.
      We check all three so the output is populated regardless of which
      endpoint produced the row.
    """
    score, reasons = scoring.score_facility(raw)

    def pick(*keys):
        for k in keys:
            v = raw.get(k)
            if v not in (None, "", "N/A"):
                return v
        return None

    return {
        "lead_score": score,
        "score_reasons": " | ".join(reasons),
        "outreach_posture": "no_events",
        "program": program,
        "registry_id": pick("RegistryID", "FRSRegistryID", "FacRegistryID"),
        "company": pick("FacName", "CWPName", "PWSName"),
        "address": pick("FacStreet", "CWPStreet"),
        "city": pick("FacCity", "CWPCity", "CitiesServed"),
        "state": pick("FacState", "CWPState", "StateCode"),
        "zip": pick("FacZip", "CWPZip", "ZipCodesServed"),
        "county": pick("FacCounty", "CWPCounty", "CountiesServed"),
        "naics": pick("FacNAICSCodes", "CWPNAICSCodes", "CWANAICS"),
        "sic": pick("FacSICCodes", "CWPSICCodes", "CWASICs"),
        # SDWA-only context — present on the API path via SDW_WANTED_COLUMNS,
        # absent for CWA leads (returns None cleanly). population_served also
        # feeds scoring.rule_population_served.
        "population_served": pick("PopulationServedCount"),
        "system_type": pick("PWSTypeDesc"),
        "owner_type": pick("OwnerDesc"),
        "primary_source": pick("PrimarySourceDesc"),
        "permit_id": pick("SourceID", "NPDESPermitNumber", "NPDESId", "PWSId"),
        # Real EPA field names (verified via *.metadata). SDW uses unprefixed
        # names (SNC, Feas, QtrsWithVio); CWA uses CWP-prefixed names.
        "snc_status": pick("CWPSNCStatus", "SNC"),
        "snc_status_date": pick("CWPSNCStatusDate"),
        "snc_event": pick("CWPSNCEventDesc"),
        "violation_status": pick("CWPViolStatus", "CurrVioFlag", "VioFlag"),
        "quarters_in_violation": pick("CWPQtrsWithNC", "QtrsWithVio"),
        "quarters_in_snc": pick("CWPQtrsWithSNC", "QtrsWithSNC"),
        "compliance_history_13q": pick("CWP13qtrsComplHistory",
                                       "SDWA3yrComplQtrsHistory"),
        "formal_actions_5yr": pick("CWPFormalEaCnt", "Feas"),
        "informal_actions_5yr": pick("CWPInformalEnfActCount", "Ifea"),
        "total_penalties_usd": pick("CWPTotalPenalties"),
        "last_penalty_date": pick("CWPDateLastPenalty"),
        "last_inspection_days_ago": pick("CWPDaysLastInspection"),
        "missing_dmr_quarters": pick("MissDMRQtrs"),
        "echo_url": ("https://echo.epa.gov/detailed-facility-report?fid="
                     + str(pick("RegistryID", "FRSRegistryID") or "")),
        # Tag columns (filled by the phase-2 augmentation after drill-down).
        # We initialise them here so every row has the same key set when
        # csv.DictWriter looks at the first row to build the header.
        "tag_active_snc": False,
        "tag_treatment_technique": False,
        "tag_mcl_violation": False,
        "tag_lead_copper": False,
        "tag_major_facility": False,
        "tag_only_resolved_events": False,
        "tag_chemtreat_high_relevance": False,
        # Internal: raw EPA dict, needed by phase-2 re-scoring. Stripped
        # before CSV write. Double-underscore signals "do not serialize".
        "__raw": raw,
    }


# How many consecutive HTTP 429s from EPA before we give up on the
# remaining candidates in a fine-comb pass. EPA's throttle is by source
# IP over a rolling window — once we trip it, the only fix is to wait
# (or change IP). Grinding through thousands of leads at 1–2s/call when
# every one comes back 429 burns hours, produces zero events, and
# delays persistence of the bulk-derived data we already have in
# memory. 20 in a row is the empirical "throttle is on, not a fluke"
# threshold — observed during the 2026-06-02 nationwide run where
# 1,700+ consecutive CWA calls 429'd without a single 200 in between.
THROTTLE_STREAK_THRESHOLD = 20


def _is_http_429(exc: BaseException) -> bool:
    """True if `exc` is a requests HTTPError carrying a 429 response.

    Kept narrow on purpose: only 429 increments the streak. Other
    errors (network drops, JSON decode, 5xx server errors) are
    transient and shouldn't trigger short-circuit — those facilities
    are individually broken, not a sign that EPA is rate-limiting us
    as a client.
    """
    # Defer the import so pipeline.py works in environments that
    # haven't pulled `requests` (the bulk loader uses it; tests can
    # synthesize duck-typed exceptions without it).
    try:
        import requests
    except ImportError:
        return False
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) == 429


def _short_circuit_remaining(remaining_leads,
                             failed_out: set | None,
                             missed_out: list[dict] | None,
                             reason: str,
                             prior_streaks: dict[tuple[str, str], int] | None = None,
                             now: datetime | None = None) -> int:
    """Mark every still-unattempted candidate as failed and return the
    count. Called when the 429 streak crosses
    THROTTLE_STREAK_THRESHOLD. The viewer's run-health card surfaces
    these as 'lookup failed — re-run later' rather than 'no records
    on file', which is the truthful state: we never tried them
    because EPA was throttling us.

    When `prior_streaks` is supplied, each short-circuited lead is
    also marked with `last_drilldown_outcome='lookup_failed'` so the
    new drill-down-state columns reflect "we never tried them this
    run" — the Snowflake eligibility view will queue them for re-run
    after `DRILLDOWN_BACKOFF['lookup_failed']`."""
    if prior_streaks is None:
        prior_streaks = {}
    skipped = 0
    ts = now or datetime.utcnow()
    for lead in remaining_leads:
        # Same per-program eligibility checks the drill loops use — a
        # CWA-only loop shouldn't penalize SDWA leads (and vice versa)
        # that were in the candidate list for the OTHER drill function.
        key = (lead.get("registry_id"), lead.get("program"))
        if failed_out is not None and key[0]:
            failed_out.add(key)
        if missed_out is not None:
            missed_out.append(lead)
        _record_drilldown_outcome(lead, "lookup_failed", prior_streaks, ts)
        skipped += 1
    if skipped:
        log.warning("%s — %d candidate(s) marked as lookup_failed for re-run",
                    reason, skipped)
    return skipped


def _drill_cwa(leads: list[dict], start: str, end: str,
               events_out: list[dict],
               inter_call_sleep: float,
               missed_out: list[dict] | None,
               failed_out: set | None = None,
               prior_streaks: dict[tuple[str, str], int] | None = None) -> int:
    """Drill CWA effluent exceedances for each lead in `leads`.

    Appends new events to `events_out` (shared with the SDWA path).
    Leads where the drill returned 0 events go into `missed_out` if
    supplied — the caller uses that list to schedule a second pass.
    Returns the count of leads that yielded ≥1 event.

    `failed_out`, if supplied, records the `(registry_id, program)` of any
    lead whose drill *raised* (timeout / connection drop / bot-block) — as
    opposed to returning cleanly with no rows. The two outcomes look
    identical in `missed_out`, but mean very different things to a user:
    a raised drill is incomplete and worth re-running, while a clean-empty
    one usually means the facility genuinely has no effluent exceedances on
    file (e.g. reporting-only or stormwater-general-permit noncompliance).
    Pass the same set through every pass; a later success/clean-empty
    discards a key added by an earlier failed attempt, so the set reflects
    each lead's *final* outcome. (A silently-throttled HTTP-200-empty
    response can't be told from genuine no-data here and counts as the
    latter — the effluent endpoint has no stub signature like DFR's.)

    Throttle short-circuit: if EPA returns HTTP 429 on
    THROTTLE_STREAK_THRESHOLD consecutive calls, the loop breaks and
    every remaining eligible candidate is marked as failed via
    `_short_circuit_remaining`. This stops a wedged fine-comb from
    burning hours of wall-clock for zero events.

    `prior_streaks`, if supplied, also enables per-row drill-down
    state writes to each touched lead (via `_record_drilldown_outcome`).
    Callers that want the Snowflake-side rerun loop to work should
    always pass it; callers that don't (existing tests) get a
    no-op pass-through.
    """
    if prior_streaks is None:
        prior_streaks = {}
    drilled = 0
    streak = 0
    for i, lead in enumerate(leads):
        if lead["program"] != "CWA" or not lead.get("permit_id"):
            continue
        before = len(events_out)
        errored = False
        try:
            for ev in echo_client.fetch_npdes_violation_events(
                    lead["permit_id"], start, end):
                ev["registry_id"] = lead["registry_id"]
                ev["program"] = "CWA"
                ev["company"] = lead["company"]
                ev["status"] = "Unresolved"
                ev["data_lag_note"] = CWA_LAG_NOTE
                events_out.append(ev)
            streak = 0   # any non-raising call resets the streak
        except Exception as e:
            errored = True
            log.warning("CWA event fetch failed for %s: %s", lead["permit_id"], e)
            streak = streak + 1 if _is_http_429(e) else 0
        # Outcome: 'with_events' if any landed, 'lookup_failed' on raise,
        # else 'no_data' (clean empty). Record before missed/failed list
        # mutation so the lead's outcome is the FINAL one for this pass.
        if len(events_out) > before:
            outcome = "with_events"
            drilled += 1
        elif errored:
            outcome = "lookup_failed"
            if missed_out is not None:
                missed_out.append(lead)
        else:
            outcome = "no_data"
            if missed_out is not None:
                missed_out.append(lead)
        _record_drilldown_outcome(lead, outcome, prior_streaks,
                                   datetime.utcnow())
        if failed_out is not None:
            key = (lead["registry_id"], lead["program"])
            failed_out.add(key) if errored else failed_out.discard(key)
        if streak >= THROTTLE_STREAK_THRESHOLD:
            _short_circuit_remaining(
                (L for L in leads[i + 1:]
                 if L["program"] == "CWA" and L.get("permit_id")),
                failed_out, missed_out,
                f"CWA fine-comb: {streak} consecutive HTTP 429s from EPA — "
                f"aborting remaining drills",
                prior_streaks=prior_streaks,
            )
            break
        time.sleep(inter_call_sleep)
    return drilled


def _drill_sdwa(leads: list[dict], events_out: list[dict],
                inter_call_sleep: float,
                missed_out: list[dict] | None,
                failed_out: set | None = None,
                prior_streaks: dict[tuple[str, str], int] | None = None) -> int:
    """Same pattern for SDWA leads via the DFR endpoint.

    `failed_out` semantics match `_drill_cwa`: it records leads whose drill
    raised (vs. returned cleanly empty). The DFR path does detect silent
    throttle stubs and raises a warning, but `fetch_sdwa_violation_events`
    swallows that into a `[]` return, so here too only a raised exception
    (connection drop / read timeout) marks a lead as failed.

    `prior_streaks` semantics match `_drill_cwa` — enables per-row
    drill-down state writes when supplied.

    Same THROTTLE_STREAK_THRESHOLD short-circuit as `_drill_cwa`.
    """
    if prior_streaks is None:
        prior_streaks = {}
    drilled = 0
    streak = 0
    for i, lead in enumerate(leads):
        if lead["program"] != "SDWA" or not lead.get("registry_id"):
            continue
        before = len(events_out)
        errored = False
        try:
            for ev in echo_client.fetch_sdwa_violation_events(lead["registry_id"]):
                ev["company"] = lead["company"]
                ev["data_lag_note"] = SDWA_LAG_NOTE
                events_out.append(ev)
            streak = 0
        except Exception as e:
            errored = True
            log.warning("SDWA event fetch failed for %s: %s",
                        lead["registry_id"], e)
            streak = streak + 1 if _is_http_429(e) else 0
        if len(events_out) > before:
            outcome = "with_events"
            drilled += 1
        elif errored:
            outcome = "lookup_failed"
            if missed_out is not None:
                missed_out.append(lead)
        else:
            outcome = "no_data"
            if missed_out is not None:
                missed_out.append(lead)
        _record_drilldown_outcome(lead, outcome, prior_streaks,
                                   datetime.utcnow())
        if failed_out is not None:
            key = (lead["registry_id"], lead["program"])
            failed_out.add(key) if errored else failed_out.discard(key)
        if streak >= THROTTLE_STREAK_THRESHOLD:
            _short_circuit_remaining(
                (L for L in leads[i + 1:]
                 if L["program"] == "SDWA" and L.get("registry_id")),
                failed_out, missed_out,
                f"SDWA fine-comb: {streak} consecutive HTTP 429s from EPA — "
                f"aborting remaining drills",
                prior_streaks=prior_streaks,
            )
            break
        time.sleep(inter_call_sleep)
    return drilled


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        log.info("(no rows for %s)", path.name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Strip internal keys (double-underscore prefix) before serializing.
    # Used to carry the raw EPA dict for phase-2 re-scoring without
    # leaking it into the CSV.
    fieldnames = [k for k in rows[0].keys() if not k.startswith("__")]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows -> %s", len(rows), path)


# ---------------------------------------------------------------- main

def _write_lag_notice(out_dir: Path) -> None:
    """Drop a plain-text notice in the output directory each run so anyone
    opening the CSVs sees the lag info before they open Excel."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "READ_ME_FIRST.txt").write_text(
        "EPA WATER VIOLATION DATA - REPORTING LAG NOTICE\n"
        "=================================================\n\n"
        + SDWA_LAG_NOTE + "\n\n"
        + CWA_LAG_NOTE + "\n\n"
        "Generated: " + datetime.utcnow().isoformat(timespec="seconds") + "Z\n"
    )


def _run_output_dir(base_out: Path, command: str, states: list[str] | None,
                    run_start_ts: str) -> Path:
    """Per-run output folder so successive runs don't overwrite each other.

    Layout: ``<base_out>/<command>_<scope>_<YYYYMMDD-HHMMSS>/``. The SQLite
    DB stays the cross-run source of truth; each folder just holds one
    run's CSV snapshot, run_health.json, and READ_ME_FIRST.txt. `scope` is
    the joined state list (or "nationwide" when there's no state filter),
    so a bulk nationwide run and a targeted pipeline run land side by side
    instead of clobbering the same filenames in `out/`.
    """
    if states:
        scope = "-".join(states)
        if len(scope) > 40:           # keep folder names sane for 20+ states
            scope = f"{len(states)}states"
    else:
        scope = "nationwide"
    # run_start_ts is ISO "2026-05-27T12:15:00"; compact to "20260527-121500".
    stamp = run_start_ts.replace("-", "").replace(":", "").replace("T", "-")
    run_dir = base_out / f"{command}_{scope}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def run(states: list[str], out_dir: Path, db_path: Path) -> None:
    print(LAG_BANNER)

    # Capture WARNING-and-above for run_health.json (bot-block, throttle,
    # drill-down miss summary). Removed in finally so the handler doesn't
    # leak into subsequent tests/processes sharing the root logger.
    warning_collector = _health.WarningCollector()
    chemtreat_logger = logging.getLogger("chemtreat")
    chemtreat_logger.addHandler(warning_collector)
    try:
        _run_inner(states, out_dir, db_path, warning_collector)
    finally:
        chemtreat_logger.removeHandler(warning_collector)


def _run_inner(states: list[str], out_dir: Path, db_path: Path,
               warning_collector: "_health.WarningCollector") -> None:
    # ---- 1. Pull and score everything in territory --------------------
    leads: list[dict] = []
    for st in states:
        log.info("[%s] querying CWA across %d NAICS prefixes…",
                 st, len(TARGET_NAICS))
        for naics in TARGET_NAICS:
            try:
                for fac in echo_client.find_cwa_violators(st, naics):
                    leads.append(_flatten_facility(fac, "CWA"))
                time.sleep(0.4)
            except Exception as e:
                log.warning("CWA %s/%s failed: %s", st, naics, e)

        log.info("[%s] querying SDWA…", st)
        try:
            for sys_ in echo_client.find_sdwa_violators(st):
                leads.append(_flatten_facility(sys_, "SDWA"))
            time.sleep(0.4)
        except Exception as e:
            log.warning("SDWA %s failed: %s", st, e)

    # Dedupe on (registry_id, program); keep the higher-scored row
    seen: dict[tuple, dict] = {}
    for r in leads:
        key = (r["registry_id"], r["program"])
        if r["registry_id"] and (key not in seen
                                 or r["lead_score"] > seen[key]["lead_score"]):
            seen[key] = r
    leads = sorted(seen.values(), key=lambda r: r["lead_score"], reverse=True)
    log.info("Found %d unique facilities", len(leads))

    # ---- 2. Drill into individual violation events --------------------
    end = datetime.utcnow().strftime("%m/%d/%Y")
    start = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")

    events: list[dict] = []
    high_value = [L for L in leads if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE]
    log.info("Drilling into events for %d high-value leads (score >= %d)",
             len(high_value), EVENT_DRILLDOWN_MIN_SCORE)

    # Leads whose FINAL drill attempt raised (timeout/connection/bot-block),
    # as opposed to returning cleanly empty. Drives the run-health split
    # between "re-run these" and "genuinely no data on file". Shared across
    # both passes; a later success/clean-empty clears an earlier failure.
    failed_keys: set = set()

    # Per-row drill-down state — loaded once before the loops so each
    # 'lookup_failed' outcome increments the right base streak. Feeds
    # _record_drilldown_outcome inside _drill_cwa / _drill_sdwa.
    with snapshot.open_db(db_path) as conn:
        prior_streaks = snapshot.load_prior_drilldown_state(conn)

    # 2a. CWA effluent exceedances (per-permit, fast endpoint)
    cwa_missed: list[dict] = []   # leads we couldn't drill — second-pass candidates
    cwa_drilled = _drill_cwa(high_value, start, end, events,
                             inter_call_sleep=0.3, missed_out=cwa_missed,
                             failed_out=failed_keys,
                             prior_streaks=prior_streaks)
    log.info("Drilled %d CWA permits (%d missed; will retry)",
             cwa_drilled, len(cwa_missed))

    # 2b. SDWA violation history (per-system, via the DFR endpoint).
    # DFR is heavier and throttles around call #15 at 0.5s pace — bumped
    # to 1.0s inter-call sleep in the main pass; failed drilldowns get a
    # second pass with even longer spacing.
    sdwa_missed: list[dict] = []
    sdwa_drilled = _drill_sdwa(high_value, events,
                               inter_call_sleep=1.0, missed_out=sdwa_missed,
                               failed_out=failed_keys,
                               prior_streaks=prior_streaks)
    log.info("Drilled %d SDWA systems (%d missed; will retry)",
             sdwa_drilled, len(sdwa_missed))

    # 2c. Second-pass for any high-value lead that came back with 0 events.
    # The thin/empty responses are usually transient EPA throttling — a
    # longer wait between calls clears them. The user's stated goal is
    # "drill down whenever needed so we don't miss violations," which is
    # what this loop is for. Cost is bounded: only the misses get
    # retried, with extra spacing.
    if cwa_missed or sdwa_missed:
        log.info("Second-pass drill-down: %d CWA + %d SDWA leads. "
                 "Sleeping 10s first to let EPA throttle clear...",
                 len(cwa_missed), len(sdwa_missed))
        time.sleep(10)
        if cwa_missed:
            cwa_recovered = _drill_cwa(cwa_missed, start, end, events,
                                       inter_call_sleep=1.0, missed_out=None,
                                       failed_out=failed_keys,
                                       prior_streaks=prior_streaks)
            log.info("Second-pass CWA recovered: %d of %d",
                     cwa_recovered, len(cwa_missed))
        if sdwa_missed:
            sdwa_recovered = _drill_sdwa(sdwa_missed, events,
                                         inter_call_sleep=2.0, missed_out=None,
                                         failed_out=failed_keys,
                                         prior_streaks=prior_streaks)
            log.info("Second-pass SDWA recovered: %d of %d",
                     sdwa_recovered, len(sdwa_missed))

    # Loud summary if drill-down miss rate is meaningful — sales-facing
    # output is much weaker without per-event detail on high-value leads.
    total_high = sum(1 for L in high_value
                     if (L["program"] == "CWA" and L.get("permit_id"))
                     or (L["program"] == "SDWA" and L.get("registry_id")))
    drilled_keys = {(ev.get("registry_id"), ev.get("program")) for ev in events}
    high_keys = {(L["registry_id"], L["program"]) for L in high_value}
    still_missing = sum(1 for k in high_keys if k not in drilled_keys)
    miss_pct = 100 * still_missing / max(total_high, 1)
    if miss_pct > 5:
        log.warning("DRILL-DOWN MISS RATE: %d/%d (%.1f%%) high-value leads "
                    "have no events after second-pass. Top of CSV will "
                    "show outreach_posture=no_events for these — score "
                    "still reflects facility-level flags but per-event "
                    "richness is missing.",
                    still_missing, total_high, miss_pct)

    # Classify the misses (failed-lookup vs no-data-on-file) for the Run
    # Health tab so the viewer can tell users what to re-run vs ignore.
    drilldown_stats = _health.summarize_drilldown(
        high_value, events, failed_keys, leads)

    # ---- 2c. Phase-2 augmentation -------------------------------------
    #
    # Re-score every lead with its drilled events, compute tag columns,
    # set the outreach_posture indicator. The initial score was facility-
    # only (right for picking which leads to drill); the final score
    # adds event-aware contributions (Treatment-Technique boost, "only
    # resolved" demote, etc.) so the top of the distribution reflects
    # actual violation status, not just summary flags.
    events_by_key: dict[tuple, list[dict]] = {}
    for ev in events:
        key = (ev.get("registry_id"), ev.get("program"))
        if key[0]:
            events_by_key.setdefault(key, []).append(ev)

    for lead in leads:
        lead_events = events_by_key.get((lead["registry_id"], lead["program"]), [])
        raw = lead.get("__raw") or {}
        new_score, new_reasons = scoring.score_facility(raw, lead_events)
        lead["lead_score"] = new_score
        lead["score_reasons"] = " | ".join(new_reasons)
        lead["outreach_posture"] = scoring.compute_outreach_posture(lead_events)
        lead.update(scoring.compute_tags(raw, lead_events))

    # Re-sort: event-aware scoring may have shuffled the top.
    leads.sort(key=lambda r: r["lead_score"], reverse=True)

    # ---- 3. Persist to DB + write standing-state CSVs from DB ---------
    #
    # `snapshot.sqlite` is the source of truth: every column the CSV
    # publishes lives in the DB. We capture one timestamp BEFORE any
    # upsert and pass it to all three writers so the dump's
    # `last_seen >= run_start_ts` filter catches every row this run
    # touched (no microsecond drift between independent utcnow() calls).
    run_start_ts = datetime.utcnow().isoformat(timespec="seconds")
    # Per-run folder so this run's CSVs don't overwrite a prior run's.
    run_dir = _run_output_dir(out_dir, "pipeline", states, run_start_ts)
    with snapshot.open_db(db_path) as conn:
        # record_run first so its returned run_id can be threaded through
        # both upserts — every touched row gets a (run_id, key) entry in
        # the run_*_membership tables. Same transaction, so a mid-run
        # failure rolls back the run row too.
        run_id = snapshot.record_run(
            conn, notes=f"states={','.join(states)}", now=run_start_ts)
        fac_diff = snapshot.diff_and_upsert_facilities(
            conn, leads, run_id, now=run_start_ts)
        viol_diff = snapshot.diff_and_upsert_violations(
            conn, events, run_id, now=run_start_ts)
        # ---- 4. Write outputs -----------------------------------------
        today = datetime.utcnow().strftime("%Y%m%d")
        _write_lag_notice(run_dir)
        snapshot.dump_facilities_csv(conn, run_dir / "all_leads.csv", run_start_ts)
        snapshot.dump_violations_csv(conn, run_dir / "violation_events.csv", run_start_ts)
    # Delta CSVs come from in-memory diff dicts — they describe what
    # CHANGED this run, which only the diff functions know.
    _write_csv(run_dir / f"new_facilities_{today}.csv", fac_diff["new"])
    _write_csv(run_dir / f"newly_snc_{today}.csv", fac_diff["newly_snc"])
    _write_csv(run_dir / f"new_violations_{today}.csv", viol_diff["new"])

    health_path = _health.write_run_health(
        run_dir,
        command="pipeline",
        states=states,
        include_events=True,
        run_start_ts=run_start_ts,
        leads=leads,
        events=events,
        fac_diff=fac_diff,
        viol_diff=viol_diff,
        drilldown_stats=drilldown_stats,
        warnings=warning_collector.records,
        event_drilldown_min_score=EVENT_DRILLDOWN_MIN_SCORE,
        secondary_drilldown_min_score=EVENT_DRILLDOWN_MIN_SCORE,
    )
    log.info("Wrote run health to %s", health_path)

    log.info("Done. %d new facilities, %d newly SNC, %d new violation events.",
             len(fac_diff["new"]), len(fac_diff["newly_snc"]),
             len(viol_diff["new"]))
    log.info("Run outputs in %s — upload all_leads.csv, violation_events.csv, "
             "and run_health.json from there.", run_dir)
    print(LAG_BANNER)   # remind them again at the end


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--states", default="TX,LA,OH,PA",
                   help="Comma-separated 2-letter state codes")
    p.add_argument("--out", default="./out", help="Output directory")
    p.add_argument("--db", default="./snapshot.sqlite", help="Snapshot DB path")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(
        states=[s.strip().upper() for s in args.states.split(",")],
        out_dir=Path(args.out),
        db_path=Path(args.db),
    )


if __name__ == "__main__":
    _cli()