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

Tuning the model is intentionally easy: every numeric weight and tier
threshold lives in the ``WEIGHTS`` dict below. Sales asks like "weight SNC
less, treatment technique more" become a one-line edit. Adding a rule is
one function + one entry in RULES/EVENT_RULES + one new key in WEIGHTS.

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


# ---------------------------------------------------------------- WEIGHTS
#
# Every numeric value that affects a score lives here. Rule bodies look up
# into this dict; nothing else in the file (and nothing in the test suite)
# should be edited to change a weight. Keys are kebab-style and grouped by
# signal class so a sales-driven edit reads naturally: "bump
# `dmr_exceedance_severe` from 15 to 18, drop `recent_inspection` to 3."
#
# What's NOT here on purpose:
#   * EVENT_DRILLDOWN_MIN_SCORE / SECONDARY_DRILLDOWN_MIN_SCORE (routing
#     thresholds, in pipeline.py / bulk_loader.py)
#   * THROTTLE_STREAK_THRESHOLD (network behavior, in pipeline.py)
#   * Viewer color tiers ≥110/80/60/40 (presentation, in index.html)
#   * Display caps (_DISPLAY_EXCEEDANCE_CAP, _MAX_PERMITTED_PARAMS_SAMPLED
#     in bulk_loader.py)
# Each of these is behavioral or presentational, not part of the score
# arithmetic, and conflating them muddles the dict's contract.

WEIGHTS: dict[str, int] = {
    # ---- enforcement-status (facility rules) -------------------------
    "snc":                            40,
    "chronic_per_quarter":             8,
    "chronic_cap":                    32,
    "formal_action":                  15,
    "major_facility":                 10,
    "penalty_large":                   8,    # ≥ $100K
    "penalty_small":                   5,    # ≥ $10K
    "penalty_large_threshold":   100_000,
    "penalty_small_threshold":    10_000,
    "recent_inspection":               5,
    "recent_inspection_max_days":    180,
    # ---- pre-violation (facility rules, bulk-only) -------------------
    "treatable_permit_per_hit":        5,
    "treatable_permit_cap":           15,
    "impaired_water":                 10,
    "impaired_parameter_match":       15,
    # ---- active-compliance (facility rules, bulk-only) ---------------
    "dmr_exceedance_severe":          15,    # > 1000%
    "dmr_exceedance_high":            12,    # ≥  200%
    "dmr_exceedance_moderate":        10,    # ≥  100%
    "dmr_exceedance_noticeable":       8,    # ≥   50%
    "dmr_exceedance_minor":            5,    # >    0%
    "dmr_threshold_severe":         1000,
    "dmr_threshold_high":            200,
    "dmr_threshold_moderate":        100,
    "dmr_threshold_noticeable":       50,
    "exceeds_treatable_composite":    15,
    # ---- SDWA revenue proxy (facility rule, API-only) ----------------
    # Population served is a direct revenue proxy: a 50K-person utility
    # is a far bigger account than a 200-person mobile-home park. The
    # field is API-only — the ECHO Exporter doesn't carry it.
    "population_large":               10,    # ≥ 50K
    "population_medium":               7,    # ≥ 10K
    "population_small":                4,    # ≥  3K
    "population_large_threshold":  50_000,
    "population_medium_threshold": 10_000,
    "population_small_threshold":   3_000,
    # ---- event rules -------------------------------------------------
    "active_open_event_per":           5,
    "active_open_event_cap":          25,
    "treatment_technique_active":     20,
    "mcl_active":                     15,
    "lead_copper_event":              10,
    "lead_copper_facility_flag":       5,
    "only_resolved_demote":          -30,
}


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

    Two detection paths, same weight:
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
        return WEIGHTS["snc"], "Significant Non-Complier (SNC text)"
    if _is_yes(f.get("SNCFlag")) or _is_yes(f.get("SeriousViolator")):
        return WEIGHTS["snc"], "Significant Non-Complier (SNC flag)"
    return None


def rule_chronic_violation(f: dict):
    """Quarters-in-violation captures duration. A facility that has been
    out of compliance for 6+ quarters is in deep trouble and likely has
    budget allocated to fix it."""
    q = _safe_int(f.get("CWPQtrsWithNC") or f.get("CWPQtrsWithSNC")
                  or f.get("QtrsWithVio") or f.get("QtrsWithSNC"))
    if q == 0:
        return None
    pts = min(q * WEIGHTS["chronic_per_quarter"], WEIGHTS["chronic_cap"])
    return pts, f"{q} quarter(s) in non-compliance"


def rule_formal_action(f: dict):
    """A formal enforcement action means EPA / state has filed paperwork.
    The facility is now legally required to remediate - they have a
    deadline and (often) a consent decree. Real spend is coming."""
    n = _safe_int(f.get("CWPFormalEaCnt") or f.get("Feas"))
    if n == 0:
        return None
    return WEIGHTS["formal_action"], f"{n} formal enforcement action(s) in last 5 yr"


def rule_major_facility(f: dict):
    """EPA classifies dischargers as 'Major' based on flow rate and
    pollutant load thresholds. Majors have bigger budgets, are watched
    more closely, and represent larger water-treatment opportunities."""
    permit_types = str(f.get("CWPPermitTypes") or "").upper()
    if "MAJOR" in permit_types and "NON-MAJOR" not in permit_types \
            and "NOT MAJOR" not in permit_types:
        return WEIGHTS["major_facility"], "Major-permit facility"
    return None


def rule_recent_penalty(f: dict):
    """A recent monetary penalty is the clearest 'they're spending money
    on compliance right now' signal."""
    amt = _safe_float(f.get("CWPTotalPenalties"))
    if amt >= WEIGHTS["penalty_large_threshold"]:
        return WEIGHTS["penalty_large"], f"Recent penalty ${amt:,.0f}"
    if amt >= WEIGHTS["penalty_small_threshold"]:
        return WEIGHTS["penalty_small"], f"Recent penalty ${amt:,.0f}"
    return None


def rule_recent_inspection(f: dict):
    """Recent inspection + open violation = EPA is actively watching.
    Translates to urgency for the facility."""
    days = _safe_int(f.get("CWPDaysLastInspection"))
    if 0 < days < WEIGHTS["recent_inspection_max_days"]:
        return WEIGHTS["recent_inspection"], f"Inspected {days} days ago"
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
    "permit_has_cyanide",
    "permit_has_chlorine_residual",
)


def rule_treatable_permit_parameter(f: dict):
    """+per_hit per ChemTreat-treatable parameter class on the facility's
    NPDES permit, capped.

    Pre-violation signal: the permit ALLOWS the facility to discharge
    something we treat. Sales call doesn't depend on a current
    violation - it's an account-research signal. Fired by bulk-loader
    runs only; pipeline.run (API path) doesn't pull permit-limit data
    today and these columns are absent there, so the rule cleanly
    returns None (no false signal)."""
    hits = sum(1 for c in PERMIT_HAS_COLS if f.get(c))
    if not hits:
        return None
    pts = min(hits * WEIGHTS["treatable_permit_per_hit"],
              WEIGHTS["treatable_permit_cap"])
    return pts, f"{hits} treatable parameter(s) on NPDES permit"


def rule_discharges_to_impaired(f: dict):
    """Points if the facility discharges into a 303(d)-impaired
    waterbody, with a higher tier when one of the facility's monitored
    effluent parameters matches a cause of that impairment.

    The parameter-match case (E90_POT_IMP_PARAMETERS populated in the
    ATTAINS summary) is rarer and stronger: the state has documented
    that THIS facility's discharge contributes to THIS waterbody's
    impairment. Permit tightening is far more likely. Without the
    match the rule still fires (lower tier) because the facility is in
    the impairment's geographic footprint."""
    if f.get("matching_impaired_parameters"):
        return (WEIGHTS["impaired_parameter_match"],
                "Discharges parameter matching impaired-waterbody cause")
    if f.get("discharges_to_impaired"):
        return (WEIGHTS["impaired_water"],
                "Discharges to 303(d) impaired waterbody")
    return None


def rule_recent_dmr_exceedance(f: dict):
    """Tiered severity of the worst single DMR exceedance in the loaded
    fiscal-year archive. Reads `top_exceedance_pct` populated by
    `bulk_loader.stream_dmr_exceedances`.

    Tiers correlate with real-world conversation tone:
      *   minor: 0–50% over — borderline, likely correctable in-process
      *   noticeable: 50–100% over — genuine non-compliance
      *   moderate: 100–200% over — multiple-of-limit, sustained
      *   high: 200–1000% over — severe; enforcement likely already underway
      *   severe: >1000% over — egregious; treatment system probably failing

    All weights and thresholds live in WEIGHTS. Bulk-only; pipeline.run
    (API path) doesn't pull DMR archives today."""
    pct = _safe_float(f.get("top_exceedance_pct"))
    if pct <= 0:
        return None
    if pct >= WEIGHTS["dmr_threshold_severe"]:
        return (WEIGHTS["dmr_exceedance_severe"],
                f"DMR exceedance {pct:.0f}% over limit (severe)")
    if pct >= WEIGHTS["dmr_threshold_high"]:
        return (WEIGHTS["dmr_exceedance_high"],
                f"DMR exceedance {pct:.0f}% over limit")
    if pct >= WEIGHTS["dmr_threshold_moderate"]:
        return (WEIGHTS["dmr_exceedance_moderate"],
                f"DMR exceedance {pct:.0f}% over limit")
    if pct >= WEIGHTS["dmr_threshold_noticeable"]:
        return (WEIGHTS["dmr_exceedance_noticeable"],
                f"DMR exceedance {pct:.0f}% over limit")
    return (WEIGHTS["dmr_exceedance_minor"],
            f"DMR exceedance {pct:.0f}% over limit")


def rule_exceeds_treatable_parameter(f: dict):
    """Points when the facility's exceeded parameters overlap with the
    ChemTreat-treatable classes their permit covers.

    This is the strongest single signal in the system. It's no longer
    "pre-violation" — it's "permit covers phosphorus AND they're
    currently exceeding phosphorus." Sales call writes itself: "we
    noticed you exceeded your phosphorus limit by X% last quarter and
    your permit covers phosphorus — we make the chemistry that fixes
    that."

    `exceeded_treatable_parameters_text` is pipe-joined treatable
    class names (e.g. "bod | phosphorus | metals"). PERMIT_HAS_COLS
    is the canonical set of class column names. Intersection is the
    match.
    """
    raw = f.get("exceeded_treatable_parameters_text") or ""
    if not raw:
        return None
    exceeded = {c.strip() for c in raw.split("|") if c.strip()}
    permitted = {c.replace("permit_has_", "")
                 for c in PERMIT_HAS_COLS if f.get(c)}
    matches = sorted(exceeded & permitted)
    if not matches:
        return None
    return (WEIGHTS["exceeds_treatable_composite"],
            "Exceeding permitted, ChemTreat-treatable parameter: "
            + ", ".join(matches))


def rule_population_served(f: dict):
    """SDWA-only revenue proxy. Tiered by `PopulationServedCount`:
    a 50K-person utility is a meaningfully larger account than a
    200-person mobile-home park, regardless of compliance status.

    API-only. ECHO Exporter doesn't expose population at the facility
    level, so bulk SDWA leads cleanly return None here (same pattern
    as the bulk-only pre-violation rules going the other direction)."""
    n = _safe_int(f.get("PopulationServedCount"))
    if n >= WEIGHTS["population_large_threshold"]:
        return (WEIGHTS["population_large"],
                f"Serves {n:,} people (major system)")
    if n >= WEIGHTS["population_medium_threshold"]:
        return WEIGHTS["population_medium"], f"Serves {n:,} people"
    if n >= WEIGHTS["population_small_threshold"]:
        return WEIGHTS["population_small"], f"Serves {n:,} people"
    return None


# ---------------------------------------------------------------- event rules

def rule_active_open_events(_f: dict, events: list[dict]):
    """Open events (Unaddressed/Unresolved) = current violations with no
    remediation underway. These are the most callable leads."""
    n = sum(1 for e in events if _is_open_event(e))
    if n == 0:
        return None
    pts = min(n * WEIGHTS["active_open_event_per"],
              WEIGHTS["active_open_event_cap"])
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
    return (WEIGHTS["treatment_technique_active"],
            f"{n} active Treatment Technique violation(s)")


def rule_health_based_mcl_active(_f: dict, events: list[dict]):
    """MCL (Maximum Contaminant Level) violations are health-based, high
    urgency for the facility, and ChemTreat-relevant when the contaminant
    is treatable (most are)."""
    n = sum(1 for e in events
            if "MAXIMUM CONTAMINANT" in str(e.get("violation_category", "")).upper()
            and _is_active_event(e))
    if n == 0:
        return None
    return WEIGHTS["mcl_active"], f"{n} active MCL violation(s)"


def rule_lead_copper_active(f: dict, events: list[dict]):
    """Lead-and-Copper Rule violations are a specific, high-revenue
    opportunity — corrosion-control chemistry. Both event-level and
    facility-flag signals count."""
    n = sum(1 for e in events
            if "LEAD AND COPPER" in str(e.get("rule_family", "")).upper()
            and _is_active_event(e))
    if n > 0:
        return (WEIGHTS["lead_copper_event"],
                f"{n} active Lead/Copper Rule violation(s)")
    # Fall back to the facility-level flags ECHO surfaces for SDWA.
    if _is_yes(f.get("PbViol")) or _is_yes(f.get("CuViol")) \
            or _is_yes(f.get("LeadAndCopperViol")):
        return (WEIGHTS["lead_copper_facility_flag"],
                "Lead/Copper history (facility flag)")
    return None


def rule_only_resolved_demote(_f: dict, events: list[dict]):
    """If a facility has events but ALL are Resolved/Archived, sales
    should not cold-call — per MEMORY.md, those are 'they fixed it'
    cases. Demote (negative score) so they sort below actively-open
    leads instead of mixing in at score=87."""
    if not events:
        return None
    if all(_event_status(e) in _INACTIVE_STATUSES for e in events):
        return (WEIGHTS["only_resolved_demote"],
                "All drilled events Resolved/Archived (verify before outreach)")
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
    rule_recent_dmr_exceedance,
    rule_exceeds_treatable_parameter,
    rule_population_served,
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

    # DMR exceedance tags. tag_recent_exceedance is True for any
    # populated top_exceedance_pct > 0. tag_exceeds_treatable_parameter
    # is the composite: facility is permitted on AND currently
    # exceeding at least one of the same treatable classes. This is
    # the strongest single tag the system produces.
    pct = facility.get("top_exceedance_pct")
    try:
        tag_recent_exc = pct is not None and float(pct) > 0
    except (TypeError, ValueError):
        tag_recent_exc = False
    raw_exc = facility.get("exceeded_treatable_parameters_text") or ""
    exceeded_set = {c.strip() for c in str(raw_exc).split("|") if c.strip()}
    permitted_set = {c.replace("permit_has_", "")
                     for c in PERMIT_HAS_COLS if facility.get(c)}
    tag_exceeds_treatable = bool(exceeded_set & permitted_set)

    # Composite — "if a rep had one filter, this is it". The pre-
    # violation signals (treatable_permit, param_match) AND the
    # active-compliance signal (exceeds_treatable) all OR-include on
    # the positive side. Resolved-only events still demote to False
    # to preserve the do-not-call guardrail.
    tag_high_rel = (
        (tag_active_snc or tag_tt or tag_mcl or tag_lc
         or tag_treatable_permit or tag_param_match
         or tag_exceeds_treatable)
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
        "tag_recent_exceedance": tag_recent_exc,
        "tag_exceeds_treatable_parameter": tag_exceeds_treatable,
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
