"""
scoring.py
==========
Lead scoring with an explicit, auditable rationale.

Every score is the sum of contributions from named rules. Sales sees both
the total and the per-rule breakdown in the CSV so they can read 'this is
a 78 because…' and challenge any number that doesn't make sense.

Two rule families:
  * RULES — read only the facility-summary fields ECHO returns from
    `get_qid` (CWPSNCStatus, CWPQtrsWithNC, Feas, …). These run first and
    decide which facilities are worth drilling for individual events.
  * EVENT_RULES — read both the facility AND its drilled violation events.
    These run after drill-down and capture status-awareness (Unaddressed
    vs Resolved), ChemTreat-specific categories (Treatment Technique,
    MCL, Lead/Copper), and the "all resolved => do not call" demotion.

Tuning the model is intentionally easy: edit RULES / EVENT_RULES below.
Each rule is a plain function. Adding one is one function + one list entry.

This module also exposes two view-builders the pipeline merges into each
output row:
  * compute_tags()             - boolean filters for Excel
  * compute_outreach_posture() - one-word "should I call?" indicator
"""

from __future__ import annotations

from typing import Callable

# A facility rule reads the raw EPA dict only; an event rule also sees the
# drilled violation events for that facility. Both return (points, reason)
# or None to skip. Negative points are allowed (used for "do not call"
# demotions).
Rule = Callable[[dict], tuple[int, str] | None]
EventRule = Callable[[dict, list[dict]], tuple[int, str] | None]


# ---------------------------------------------------------------- helpers

def _is_yes(v) -> bool:
    return str(v or "").upper() in ("Y", "YES", "S", "SIG", "TRUE", "1")


def _safe_int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_status(e: dict) -> str:
    return str(e.get("status") or "").upper()


# Event status vocabulary. SDWA uses "Unaddressed/Addressed/Resolved/Archived";
# CWA NPDES events don't carry per-event status, so we default them to
# "Unresolved" in the pipeline. Both UNADDRESSED and UNRESOLVED mean "open
# violation, no remediation underway" for our purposes.
_ACTIVE_STATUSES = {"UNADDRESSED", "UNRESOLVED", "OPEN"}
_ENFORCEMENT_STATUSES = {"ADDRESSED"}
_INACTIVE_STATUSES = {"RESOLVED", "ARCHIVED"}


def _is_active_event(e: dict) -> bool:
    """True if the event is open / not closed out. Empty status counts
    as active to be safe — sales can verify before calling."""
    return _event_status(e) not in _INACTIVE_STATUSES


def _is_open_event(e: dict) -> bool:
    """Strict version: explicitly Unaddressed/Unresolved/Open. Used by the
    rule that rewards genuinely-open violations (vs. unknown-status ones)."""
    return _event_status(e) in _ACTIVE_STATUSES


# ---------------------------------------------------------------- facility rules

def rule_significant_violator(f: dict):
    """SNC = Significant Non-Complier. EPA's red-flag designation.
    A facility doesn't get tagged SNC for a one-off; this means recurring
    or large exceedances. Highest-value signal we have.

    Two detection paths, same +40 weight:
      * Text: CWPSNCStatus / SNC carries descriptive text — we match
        by keyword.
      * Flag: SNCFlag / SeriousViolator is a Y/N boolean. The bulk
        loader sets SNCFlag on BOTH CWA and SDWA program shapes when
        the corresponding `*_SNC_FLAG` is "Y" — historically this
        branch was SDWA-only, hence the now-misleading old "(SDWA)"
        label. The reason string distinguishes the branches so a
        reader can see which signal fired without inferring program
        from somewhere else in the row.
    """
    snc_status = str(f.get("CWPSNCStatus") or f.get("SNC") or "").upper()
    if any(t in snc_status for t in
           ("SIGNIFICANT", "SNC", "CATEGORY I", "ENFORCEMENT PRIORITY")):
        return 40, "Significant Non-Complier (SNC text)"
    if _is_yes(f.get("SNCFlag")) or _is_yes(f.get("SeriousViolator")):
        return 40, "Significant Non-Complier (SNC flag)"
    return None


def rule_chronic_violation(f: dict):
    """Quarters-in-violation captures duration. A facility that has been
    out of compliance for 6+ quarters is in deep trouble and likely has
    budget allocated to fix it."""
    q = _safe_int(f.get("CWPQtrsWithNC") or f.get("CWPQtrsWithSNC")
                  or f.get("QtrsWithVio") or f.get("QtrsWithSNC"))
    if q == 0:
        return None
    pts = min(q * 8, 32)
    return pts, f"{q} quarter(s) in non-compliance"


def rule_formal_action(f: dict):
    """A formal enforcement action means EPA / state has filed paperwork.
    The facility is now legally required to remediate - they have a
    deadline and (often) a consent decree. Real spend is coming."""
    n = _safe_int(f.get("CWPFormalEaCnt") or f.get("Feas"))
    if n == 0:
        return None
    return 15, f"{n} formal enforcement action(s) in last 5 yr"


def rule_major_facility(f: dict):
    """EPA classifies dischargers as 'Major' based on flow rate and
    pollutant load thresholds. Majors have bigger budgets, are watched
    more closely, and represent larger water-treatment opportunities."""
    permit_types = str(f.get("CWPPermitTypes") or "").upper()
    if "MAJOR" in permit_types and "NON-MAJOR" not in permit_types \
            and "NOT MAJOR" not in permit_types:
        return 10, "Major-permit facility"
    return None


def rule_recent_penalty(f: dict):
    """A recent monetary penalty is the clearest 'they're spending money
    on compliance right now' signal."""
    amt = _safe_float(f.get("CWPTotalPenalties"))
    if amt >= 100_000:
        return 8, f"Recent penalty ${amt:,.0f}"
    if amt >= 10_000:
        return 5, f"Recent penalty ${amt:,.0f}"
    return None


def rule_recent_inspection(f: dict):
    """Recent inspection + open violation = EPA is actively watching.
    Translates to urgency for the facility."""
    days = _safe_int(f.get("CWPDaysLastInspection"))
    if 0 < days < 180:
        return 5, f"Inspected {days} days ago"
    return None


# Columns set by bulk_loader's permit-limits enrichment. Listed here
# (not inline in the rule) so compute_tags can reuse the same list and
# nothing drifts between rule + tag if a category is added or removed.
PERMIT_HAS_COLS = (
    "permit_has_phosphorus",
    "permit_has_ammonia",
    "permit_has_tss",
    "permit_has_bod",
    "permit_has_oil_grease",
    "permit_has_metals",
    "permit_has_chlorine_residual",
)


def rule_treatable_permit_parameter(f: dict):
    """+5 per ChemTreat-treatable parameter class on the facility's
    NPDES permit, capped at +15.

    Pre-violation signal: the permit ALLOWS the facility to discharge
    something we treat. Sales call doesn't depend on a current
    violation - it's an account-research signal. Fired by bulk-loader
    runs only; pipeline.run (API path) doesn't pull permit-limit data
    today and these columns are absent there, so the rule cleanly
    returns None (no false signal)."""
    hits = sum(1 for c in PERMIT_HAS_COLS if f.get(c))
    if not hits:
        return None
    pts = min(hits * 5, 15)
    return pts, f"{hits} treatable parameter(s) on NPDES permit"


def rule_discharges_to_impaired(f: dict):
    """+10 if the facility discharges into a 303(d)-impaired waterbody,
    or +15 if at least one of the facility's monitored effluent
    parameters matches a cause of that impairment.

    The parameter-match case (E90_POT_IMP_PARAMETERS populated in the
    ATTAINS summary) is rarer and stronger: the state has documented
    that THIS facility's discharge contributes to THIS waterbody's
    impairment. Permit tightening is far more likely. Without the
    match the rule still fires (+10) because the facility is in the
    impairment's geographic footprint."""
    if f.get("matching_impaired_parameters"):
        return 15, "Discharges parameter matching impaired-waterbody cause"
    if f.get("discharges_to_impaired"):
        return 10, "Discharges to 303(d) impaired waterbody"
    return None


# ---------------------------------------------------------------- event rules

def rule_active_open_events(_f: dict, events: list[dict]):
    """Open events (Unaddressed/Unresolved) = current violations with no
    remediation underway. These are the most callable leads."""
    n = sum(1 for e in events if _is_open_event(e))
    if n == 0:
        return None
    pts = min(n * 5, 25)
    return pts, f"{n} open violation event(s)"


def rule_treatment_technique_active(_f: dict, events: list[dict]):
    """Treatment Technique violations are the single highest-relevance
    category for ChemTreat — they're what their chemistry products fix.
    Only count active ones (status != Resolved/Archived)."""
    n = sum(1 for e in events
            if "TREATMENT TECHNIQUE" in str(e.get("violation_category", "")).upper()
            and _is_active_event(e))
    if n == 0:
        return None
    return 20, f"{n} active Treatment Technique violation(s)"


def rule_health_based_mcl_active(_f: dict, events: list[dict]):
    """MCL (Maximum Contaminant Level) violations are health-based, high
    urgency for the facility, and ChemTreat-relevant when the contaminant
    is treatable (most are)."""
    n = sum(1 for e in events
            if "MAXIMUM CONTAMINANT" in str(e.get("violation_category", "")).upper()
            and _is_active_event(e))
    if n == 0:
        return None
    return 15, f"{n} active MCL violation(s)"


def rule_lead_copper_active(f: dict, events: list[dict]):
    """Lead-and-Copper Rule violations are a specific, high-revenue
    opportunity — corrosion-control chemistry. Both event-level and
    facility-flag signals count."""
    n = sum(1 for e in events
            if "LEAD AND COPPER" in str(e.get("rule_family", "")).upper()
            and _is_active_event(e))
    if n > 0:
        return 10, f"{n} active Lead/Copper Rule violation(s)"
    # Fall back to the facility-level flags ECHO surfaces for SDWA.
    if _is_yes(f.get("PbViol")) or _is_yes(f.get("CuViol")) \
            or _is_yes(f.get("LeadAndCopperViol")):
        return 5, "Lead/Copper history (facility flag)"
    return None


def rule_only_resolved_demote(_f: dict, events: list[dict]):
    """If a facility has events but ALL are Resolved/Archived, sales
    should not cold-call — per MEMORY.md, those are 'they fixed it'
    cases. Demote (negative score) so they sort below actively-open
    leads instead of mixing in at score=87."""
    if not events:
        return None
    if all(_event_status(e) in _INACTIVE_STATUSES for e in events):
        return -30, "All drilled events Resolved/Archived (verify before outreach)"
    return None


# ---------------------------------------------------------------- rule lists
#
# Order is for readability only - the total is sum-of-contributions, so
# rule ordering can't change the score. Adding a new rule = define a fn
# above and append it here.

RULES: list[Rule] = [
    rule_significant_violator,
    rule_chronic_violation,
    rule_formal_action,
    rule_major_facility,
    rule_recent_penalty,
    rule_recent_inspection,
    rule_treatable_permit_parameter,
    rule_discharges_to_impaired,
]

EVENT_RULES: list[EventRule] = [
    rule_active_open_events,
    rule_treatment_technique_active,
    rule_health_based_mcl_active,
    rule_lead_copper_active,
    rule_only_resolved_demote,
]


# ---------------------------------------------------------------- API

def score_facility(facility: dict,
                   events: list[dict] | None = None
                   ) -> tuple[int, list[str]]:
    """Return (total_score, list of human-readable reasons).

    `events` is optional. When None (or empty), only facility rules run —
    which is the right behavior for the initial scoring pass before the
    drill-down threshold is applied. When events are supplied, the event
    rules add their contribution and the total reflects actual violation
    status, not just facility-level summary flags.

    There's no MAX_SCORE cap. The previous 100-point ceiling flattened
    the top of the distribution (99 ties at 87 in the pre-event TX run).
    Sales would rather see "this one is a 142" than have a third of the
    inventory collapsed onto the same number.
    """
    events = events or []
    total = 0
    reasons: list[str] = []
    for rule in RULES:
        result = rule(facility)
        if result is None:
            continue
        pts, why = result
        total += pts
        reasons.append(f"{'+' if pts >= 0 else ''}{pts}: {why}")
    for erule in EVENT_RULES:
        result = erule(facility, events)
        if result is None:
            continue
        pts, why = result
        total += pts
        reasons.append(f"{'+' if pts >= 0 else ''}{pts}: {why}")
    return total, reasons


# ---------------------------------------------------------------- views
#
# Tags and outreach_posture are projections of (facility, events) that
# sales filters/sorts on. They're not part of the score; they're meant
# to let a non-technical reader pare 7,000 rows into the 50 they want
# without reading reason strings.

def compute_tags(facility: dict, events: list[dict] | None = None) -> dict:
    """Boolean filters for the CSV.

    The composite `tag_chemtreat_high_relevance` is the "if a rep had
    one filter, this is it" tag — it's True when at least one of the
    high-relevance categories fires AND the facility isn't a do-not-call.
    """
    events = events or []

    snc_text = str(facility.get("CWPSNCStatus") or facility.get("SNC") or "").upper()
    tag_active_snc = (
        any(t in snc_text for t in
            ("SIGNIFICANT", "SNC", "CATEGORY I", "ENFORCEMENT PRIORITY"))
        or _is_yes(facility.get("SNCFlag"))
        or _is_yes(facility.get("SeriousViolator"))
    )

    tag_tt = any(
        "TREATMENT TECHNIQUE" in str(e.get("violation_category", "")).upper()
        and _is_active_event(e)
        for e in events
    )
    tag_mcl = any(
        "MAXIMUM CONTAMINANT" in str(e.get("violation_category", "")).upper()
        and _is_active_event(e)
        for e in events
    )
    tag_lc = (
        any("LEAD AND COPPER" in str(e.get("rule_family", "")).upper()
            and _is_active_event(e)
            for e in events)
        or _is_yes(facility.get("PbViol"))
        or _is_yes(facility.get("CuViol"))
        or _is_yes(facility.get("LeadAndCopperViol"))
    )

    permit_types = str(facility.get("CWPPermitTypes") or "").upper()
    tag_major = (
        "MAJOR" in permit_types
        and "NON-MAJOR" not in permit_types
        and "NOT MAJOR" not in permit_types
    )

    tag_only_resolved = bool(events) and all(
        _event_status(e) in _INACTIVE_STATUSES for e in events
    )

    # Permit-limit + ATTAINS tags. Bulk-only signal; pipeline.run
    # (API path) doesn't pull these columns, so the keys are absent
    # there and the tags evaluate cleanly to False.
    tag_treatable_permit = any(facility.get(c) for c in PERMIT_HAS_COLS)
    tag_to_impaired = bool(facility.get("discharges_to_impaired"))
    tag_param_match = bool(facility.get("matching_impaired_parameters"))

    # Composite — "if a rep had one filter, this is it". The pre-violation
    # signals (treatable_permit, param_match) are OR-included so a permit
    # that ALLOWS the facility to discharge ChemTreat-treatable parameters
    # counts as high-relevance even without an open violation. Resolved-
    # only events still demote the composite to False to preserve the
    # do-not-call guardrail.
    tag_high_rel = (
        (tag_active_snc or tag_tt or tag_mcl or tag_lc
         or tag_treatable_permit or tag_param_match)
        and not tag_only_resolved
    )

    return {
        "tag_active_snc": tag_active_snc,
        "tag_treatment_technique": tag_tt,
        "tag_mcl_violation": tag_mcl,
        "tag_lead_copper": tag_lc,
        "tag_major_facility": tag_major,
        "tag_only_resolved_events": tag_only_resolved,
        "tag_treatable_permit": tag_treatable_permit,
        "tag_discharges_to_impaired": tag_to_impaired,
        "tag_impairment_parameter_match": tag_param_match,
        "tag_chemtreat_high_relevance": tag_high_rel,
    }


def compute_outreach_posture(events: list[dict] | None) -> str:
    """One-word indicator of whether to call this lead. Drives sales
    triage faster than parsing the reason string.

      active                - at least one open violation (Unaddressed)
      enforcement_underway  - Addressed events but nothing Unaddressed
      verify_first          - events exist, all are Resolved (they fixed
                              it; verify on-the-ground before outreach)
      historical            - all Archived (closed by EPA / no longer
                              counted; often recent, NOT just >5yr old)
      no_events             - no drill-down data; rely on facility score
    """
    if not events:
        return "no_events"
    statuses = {_event_status(e) for e in events}
    if statuses & _ACTIVE_STATUSES:
        return "active"
    if statuses & _ENFORCEMENT_STATUSES:
        return "enforcement_underway"
    if "RESOLVED" in statuses:
        return "verify_first"
    if "ARCHIVED" in statuses:
        return "historical"
    return "no_events"
