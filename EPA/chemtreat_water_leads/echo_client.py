"""
echo_client.py
==============
Thin, well-documented HTTP client for EPA's ECHO REST services.

EPA's ECHO (Enforcement and Compliance History Online) exposes several
REST endpoints. We use four of them:

  1. cwa_rest_services.get_facilities   - finds CWA (industrial wastewater)
                                          dischargers, filtered server-side
                                          by state / NAICS / compliance flag.
  2. sdw_rest_services.get_systems      - same idea for SDWA public water
                                          systems.
  3. eff_rest_services.get_effluent_chart
                                        - per-NPDES-permit: every Discharge
                                          Monitoring Report (DMR) reading,
                                          its permitted limit, and any
                                          individual violation events.
  4. dfr_rest_services.get_dfr          - "Detailed Facility Report" for one
                                          facility; includes the SDWA
                                          violation history we can't get
                                          out of get_systems directly.

No API key. EPA asks for "reasonable" rate limits; we sleep between calls
in the higher-level modules.

Response envelopes are inconsistent across endpoints. Each function below
unwraps the relevant part and always returns a list (possibly empty) so
callers don't have to handle None / nested dicts.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

BASE = "https://echodata.epa.gov/echo"
TIMEOUT = 60          # seconds; some queries are slow when EPA is busy
DEFAULT_PAGE = 500    # API hard cap is around 1000; 500 is safer

# DFR throttle detection. When EPA throttles us, the DFR endpoint returns
# HTTP 200 with valid JSON but a stub Results object — typically just 1
# key instead of the usual ~55. Real "no violations" responses come back
# with the full envelope, so key-density distinguishes throttle from
# legitimate emptiness.
#
# Empirically (2026-05-22): DFR throttles around call #15 at 0.5s pace.
# Once throttled, the previous 2s single-retry wasn't enough; bumped to
# (5, 15, 45) to match the general bot-block schedule.
DFR_THROTTLE_KEY_THRESHOLD = 10
DFR_RETRY_BACKOFF_SCHEDULE = (5, 15, 45)   # seconds; tried in order on thin response

# General bot-block detection. When EPA detects a "robotic or programmed
# query" (their phrasing) anywhere in echodata.epa.gov, it returns HTTP 200
# with `{"Error": {"ErrorMessage": "Your query has been identified as a
# robotic or programmed query, and has been blocked..."}}` instead of the
# usual `{"Results": {...}}`. The block clears in ~5–10 seconds. See
# MEMORY.md "EPA bot-block" entry for the discovery context.
BOT_BLOCK_SUBSTRING = "robotic or programmed query"
BOT_BLOCK_BACKOFF_SCHEDULE = (5, 15, 45)   # seconds between retries

# EPA's docs ask that programmatic clients identify themselves so traffic
# can be attributed. Without this header, our calls look like an
# unidentified scraper, which raises the bot-block probability.
USER_AGENT = (
    "chemtreat-water-leads/1.0 (lead-gen tool; "
    "https://github.com/chemtreat/water-leads)"
)


class EpaBotBlocked(RuntimeError):
    """Raised when ECHO's bot-block response persists across all retries.
    The caller can choose to fail the whole run or skip the offending
    state and continue."""


def _looks_bot_blocked(payload: dict) -> bool:
    """True if `payload` is the EPA 'robotic query' error envelope."""
    err = payload.get("Error")
    if not isinstance(err, dict):
        return False
    msg = str(err.get("ErrorMessage") or "")
    return BOT_BLOCK_SUBSTRING in msg.lower()


# ----------------------------------------------------- column metadata
#
# ECHO's get_qid endpoint returns a small default column set (~20 fields:
# facility identity + location only). The compliance fields we need for
# scoring (CWASNC, CWAQtrsWithNC, CWAFormalActionCount, etc.) are NOT in
# the default set - you have to ask for them explicitly via `qcolumns`,
# which takes a comma-separated list of column ID NUMBERS.
#
# To translate "CWASNC" -> "73" (or whatever ID it has today), we hit the
# service's metadata endpoint once per process and cache the result.
# If metadata fails, we fall back to defaults and the rows arrive without
# compliance data, which the client-side filter then catches.

# Fields we want in CWA responses. Names are best-effort - if EPA ever
# renames a column the lookup just misses (no crash, just no qcolumns
# for that field, so we silently fall back to default columns).
CWA_WANTED_COLUMNS = [
    # Identity / location
    "CWPName", "CWPStreet", "CWPCity", "CWPState", "CWPZip", "CWPCounty",
    "SourceID", "RegistryID", "FacLat", "FacLong", "FacName",
    # Classification
    "CWPNAICSCodes", "CWPSICCodes", "CWPPermitTypes",
    "CWPPermitStatusDesc",
    # Compliance signals (CWP-prefixed, verified against live metadata).
    # CWPSNCStatus carries descriptive text like "Significant/Category I
    # Noncompliance" or "No Violation Identified" - it's not a Y/N flag.
    "CWPSNCStatus", "CWPSNCStatusDate", "CWPSNCEventDesc",
    "CWPViolStatus", "ViolFlag", "VioLastYear",
    "CWPQtrsWithNC", "CWPQtrsWithSNC",
    "CWP13qtrsComplHistory",
    "CWPFormalEaCnt", "CWPInformalEnfActCount",
    "CWPTotalPenalties", "CWPDateLastPenalty",
    "CWPInspectionCount", "CWPDaysLastInspection", "CWPDateLastInspection",
    "MissDMRQtrs",
    "CWPComplianceTracking",
]

# SDW column names verified against sdw_rest_services.metadata. EPA uses
# different names than CWA - notably PWSId (not PWSID), SNC (not SDWASNC),
# Feas (formal-action count, not SDWAFormalActionCount), and the location
# fields are *Served (CitiesServed, CountiesServed, ZipCodesServed) because
# a public water system can span multiple cities. SDW has no per-system
# penalty amount field; only enforcement-action counts.
SDW_WANTED_COLUMNS = [
    # Identity / location
    "PWSName", "PWSId", "RegistryID", "StateCode",
    "CitiesServed", "CountiesServed", "ZipCodesServed",
    "PWSTypeDesc", "PWSActivityDesc", "PopulationServedCount",
    "PrimarySourceDesc", "OwnerDesc",
    # Compliance signals
    "SNC", "SNCFlag", "SeriousViolator",
    "VioFlag", "CurrVioFlag", "NewVioFlg", "HealthFlag",
    "QtrsWithVio", "QtrsWithSNC",
    "Feas", "FeaFlag", "Ifea", "IeaFlag",
    "PbViol", "CuViol", "LeadAndCopperViol",
    "SDWA3yrComplQtrsHistory", "SDWAContaminantsInCurViol",
    "SDWAContaminantsInViol3yr", "ViolationCategories", "RulesVio",
    "Insp5yrFlag", "SDWDateLastFea", "SDWDateLastIea",
]

# Module-level cache: service name -> {column_name: column_id}
_column_cache: dict[str, dict[str, str]] = {}


def _get_service_columns(service: str) -> dict[str, str]:
    """Lazily fetch and cache column metadata for an ECHO service.

    Returns {column_name: column_id_string}. Empty dict on any failure.
    Defensive parsing: the metadata response shape varies a bit across
    ECHO services, so we check several plausible keys.
    """
    if service in _column_cache:
        return _column_cache[service]

    mapping: dict[str, str] = {}
    try:
        data = _get(f"{service}.metadata", {})
        results = data.get("Results") or {}
        candidates = (
            results.get("ResultColumns"),  # the actual key EPA uses
            results.get("ColumnData"),
            results.get("ColumnSummary"),
            results.get("Columns"),
            results.get("Metadata"),
        )
        cols = next((c for c in candidates if isinstance(c, list)), [])
        for col in cols:
            if not isinstance(col, dict):
                continue
            name = (col.get("ObjectName") or col.get("ColumnName")
                    or col.get("Name"))
            cid = (col.get("ColumnID") or col.get("ColumnId")
                   or col.get("ID"))
            if name and cid is not None:
                mapping[str(name)] = str(cid)
        log.info("Discovered %d columns for %s.metadata",
                 len(mapping), service)
    except Exception as e:
        log.warning("%s metadata fetch failed (%s); using default columns",
                    service, e)

    _column_cache[service] = mapping
    return mapping


def _build_qcolumns(service: str, wanted: list[str]) -> str:
    """Translate a list of column NAMES into a comma-separated string of
    column IDs suitable for the qcolumns parameter. Names we don't know
    about are silently skipped."""
    columns = _get_service_columns(service)
    ids = [columns[name] for name in wanted if name in columns]
    return ",".join(ids)


# --------------------------------------------------------------------- core

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Lazily-built shared session so EPA can attribute our traffic and
    we don't pay TCP/TLS overhead on every call."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _session = s
    return _session


def _get(path: str, params: dict[str, Any]) -> dict:
    """One place to do HTTP. All callers go through here so retries,
    logging, and error handling stay in a single function.

    When EPA returns its "robotic or programmed query" bot-block (HTTP
    200 with `{"Error": {...}}` instead of the usual `{"Results": ...}`),
    we sleep through the BOT_BLOCK_BACKOFF_SCHEDULE and retry. The block
    typically clears in ~5s but we give it room to ride out longer
    intermittent blocks. After all retries fail we raise EpaBotBlocked
    so the caller sees a loud error, not a silent empty result.
    """
    params = {**params, "output": "JSON"}
    url = f"{BASE}/{path}"
    session = _get_session()

    for attempt, backoff in enumerate((0,) + BOT_BLOCK_BACKOFF_SCHEDULE):
        if backoff:
            log.warning("ECHO bot-block detected; sleeping %ds before retry %d/%d",
                        backoff, attempt, len(BOT_BLOCK_BACKOFF_SCHEDULE))
            time.sleep(backoff)
        log.debug("GET %s %s", url, params)
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError:
            # EPA occasionally returns HTML when overloaded; surface clearly.
            raise RuntimeError(f"Non-JSON response from {url}: {r.text[:200]}")
        if _looks_bot_blocked(payload):
            continue
        return payload

    raise EpaBotBlocked(
        f"ECHO bot-block persisted after {len(BOT_BLOCK_BACKOFF_SCHEDULE)} retries "
        f"on {url}. Increase BOT_BLOCK_BACKOFF_SCHEDULE or reduce request rate."
    )


# --------------------------------------------------------- facility search

def find_cwa_violators(
    state: str,
    naics_prefix: str | None = None,
    page_size: int = DEFAULT_PAGE,
    severe_only: bool = False,
) -> list[dict]:
    """Active CWA dischargers with any compliance signal worth a sales call.

    PARAMETER NAMING WARNING:
      EPA's `p_ncs` parameter is the NAICS FILTER (NaicS), NOT a non-
      compliance filter. The naming is unfortunate. There is also no
      `p_naics` parameter; using one is silently ignored by the API.

    Filtering strategy:
      Server-side, we only filter by state, active-permit, and NAICS
      prefix. This is intentional. EPA's server-side compliance filters
      (e.g. p_e90_count for effluent exceedances) are individually
      narrow - they catch only one violation type each, and there's no
      OR-combinator across them. Filtering server-side gives you very
      few results that match exactly one definition of "in trouble."

      Instead, we pull all active facilities in the NAICS slice and
      apply `_has_cwa_compliance_signal` client-side. That function
      returns True if ANY of the following are set: SNC flag, quarters
      in non-compliance, formal action count, violations in the 3-yr
      compliance history string, or E90 exceedance count. This matches
      the "in any kind of trouble" definition sales actually wants.

    severe_only=True:
      For the strictest filter, this adds the server-side E90 filter
      (>=1 effluent exceedance in last 3 years). Use this when you only
      want facilities with measured pollutant exceedances - a much
      smaller, higher-confidence set.
    """
    init_params = {
        "p_st": state,
        "p_act": "Y",
    }
    if naics_prefix:
        init_params["p_ncs"] = naics_prefix   # the NAICS filter, despite the name
    if severe_only:
        init_params["p_e90_count"] = "1"
        init_params["p_e90_years"] = "3"

    facilities = _qid_workflow(
        "cwa_rest_services", init_params,
        init_endpoint="get_facilities",
        array_key="Facilities",
        page_size=page_size,
        wanted_columns=CWA_WANTED_COLUMNS,
    )

    # Client-side compliance gate (the real "is this a lead?" filter).
    filtered = [f for f in facilities if _has_cwa_compliance_signal(f)]
    log.debug("CWA %s/%s: %d facilities returned -> %d passed compliance filter",
              state, naics_prefix or "*", len(facilities), len(filtered))
    return filtered


def _has_cwa_compliance_signal(f: dict) -> bool:
    """Does this facility row carry any CWA non-compliance evidence?

    CWPSNCStatus is the primary signal but it's not a Y/N flag - it
    holds descriptive text like 'Significant/Category I Noncompliance'
    or 'No Violation Identified' or 'Not Applicable'. We accept anything
    that isn't explicitly clean.
    """
    # SNC / violation status - descriptive text, not Y/N
    snc = str(f.get("CWPSNCStatus") or "").strip().upper()
    if snc and snc not in ("NO VIOLATION IDENTIFIED", "NOT APPLICABLE",
                            "UNKNOWN", "N/A", "-"):
        return True

    # CWPViolStatus is a Yes/No flag, NOT descriptive text like CWPSNCStatus.
    # "No" means no current violation; only "Yes" indicates a signal. Treating
    # this as free text (and excluding "NO VIOLATION IDENTIFIED" etc.) let
    # every clean facility through because "NO" isn't in the clean-list.
    viol = str(f.get("CWPViolStatus") or "").strip().upper()
    if viol in ("Y", "YES", "TRUE", "1"):
        return True

    # Quarter counts - CWP* names per EPA metadata
    for k in ("CWPQtrsWithNC", "CWPQtrsWithSNC", "MissDMRQtrs"):
        try:
            if int(float(f.get(k) or 0)) > 0:
                return True
        except (TypeError, ValueError):
            pass

    # Enforcement action counts
    for k in ("CWPFormalEaCnt", "CWPInformalEnfActCount"):
        try:
            if int(float(f.get(k) or 0)) > 0:
                return True
        except (TypeError, ValueError):
            pass

    # 13-quarter compliance history string (e.g. "VVCCCCCCCCCCC" = oldest->newest)
    hist = str(f.get("CWP13qtrsComplHistory") or "")
    if any(c in hist.upper() for c in ("V", "S")):
        return True

    # Violation flags
    for k in ("ViolFlag", "VioLastYear"):
        if str(f.get(k) or "").upper() in ("Y", "YES", "TRUE", "1"):
            return True

    # If we have NO CWA/CWP fields at all, this is probably a stripped
    # response - fail open and let downstream filter. With qcolumns
    # working correctly, this should never happen in practice.
    has_any_compliance_field = any(
        k.startswith(("CWP", "CWA", "Viol", "MissDMR"))
        for k in f.keys()
    )
    return not has_any_compliance_field


def find_sdwa_violators(state: str, page_size: int = DEFAULT_PAGE) -> list[dict]:
    """Active SDWA public water systems with at least one open violation.

    SDWA uses simpler filters (no equivalent of the CWA E90 count filter).
    The p_viola='Y' parameter is correct here - SDW services use it
    for "has violations" - and is documented as such.
    """
    init_params = {
        "p_st": state,
        "p_act": "Y",
        "p_viola": "Y",
    }
    return _qid_workflow(
        "sdw_rest_services", init_params,
        init_endpoint="get_systems",
        array_key="Systems",
        page_size=page_size,
        wanted_columns=SDW_WANTED_COLUMNS,
    )


# --------------------------------------------------------- diagnostic

def inspect_cwa_response(state: str = "TX", naics: str = "325",
                          limit: int = 1) -> list[dict]:
    """Print the first N raw records from a CWA query.

    Diagnostic helper: shows the actual field names EPA returns, which
    you need to know if you're changing the flatten / scoring code. The
    'expected' field names in our docs and the actual response shape
    can diverge across endpoint versions.

    Usage from a one-liner:
        python -c "from chemtreat_water_leads.echo_client import \\
                   inspect_cwa_response; inspect_cwa_response('TX', '325')"
    """
    rows = find_cwa_violators(state, naics)
    print(f"\nfind_cwa_violators({state!r}, {naics!r}) returned {len(rows)} rows")
    for i, r in enumerate(rows[:limit]):
        print(f"\n--- record {i} ---")
        for k in sorted(r.keys()):
            v = str(r[k])
            if len(v) > 70:
                v = v[:70] + "..."
            print(f"  {k:40} = {v}")
    return rows[:limit]


def _qid_workflow(
    service: str,
    init_params: dict,
    init_endpoint: str,
    array_key: str,
    page_size: int,
    wanted_columns: list[str] | None = None,
) -> list[dict]:
    """The two-call ECHO pattern: init query for a QID, then paginate.

    Step 1 (init): hits e.g. cwa_rest_services.get_facilities with our
                   filters. Response carries:
                       Results.QueryID    -- ID we'll use to paginate
                       Results.QueryRows  -- total matching facilities
                   The init response is sometimes the only call needed
                   (small result sets include the array inline).
                   We handle both shapes.

    Step 2 (paginate): repeated calls to <service>.get_qid?qid=X&pageno=N
                       until we've collected QueryRows facilities or
                       see an empty page.

    `wanted_columns` is a list of column NAMES we want in the response.
    These get translated to ECHO's numeric column-ID format via the
    metadata cache and passed as `qcolumns`. Without this, EPA returns
    a minimal default column set that omits the compliance fields.
    """
    # Step 1: init
    init_data = _get(f"{service}.{init_endpoint}", init_params)
    results = init_data.get("Results") or {}

    # If the init response already includes the array (small queries),
    # we're done in one call.
    inline = (results.get(array_key)
              or results.get("Facilities")
              or results.get("FacilityInfo")
              or results.get("Systems"))
    if isinstance(inline, list) and inline:
        return inline

    qid = results.get("QueryID") or results.get("QID")
    try:
        total = int(results.get("QueryRows") or 0)
    except (TypeError, ValueError):
        total = 0

    if not qid:
        # The bot-block path is handled in `_get` via retry; if we still
        # have no QID here it's either a 0-result query or a genuinely
        # malformed request. Dump enough of the raw response that the
        # next debugger doesn't have to instrument anything.
        msg = results.get("Message") or results.get("Error") or "unknown"
        log.warning(
            "ECHO %s: no facility array AND no QID (msg=%r, top_keys=%s, "
            "raw_init=%s). Endpoint or params may be wrong.",
            init_endpoint, msg, list(results.keys())[:10],
            str(init_data)[:400],
        )
        return []

    if total == 0:
        log.debug("ECHO %s returned QID=%s with 0 matching rows",
                  init_endpoint, qid)
        return []

    log.debug("ECHO %s returned QID=%s, %d matching rows; paginating...",
              init_endpoint, qid, total)

    # Build qcolumns string so EPA returns the columns we actually need.
    qcolumns = _build_qcolumns(service, wanted_columns or []) if wanted_columns else ""

    # Step 2: paginate via get_qid.
    # Sleep briefly between pages — pagination has no built-in throttle
    # (unlike the per-NAICS sleep in pipeline.py), and a long paginate
    # of a large state was what tripped EPA's bot-block in the first
    # place. 0.3s keeps us under the threshold without meaningfully
    # slowing nationwide runs.
    all_rows: list[dict] = []
    page = 1
    max_pages = 200   # hard cap to avoid runaway loops
    while page <= max_pages and len(all_rows) < total:
        if page > 1:
            time.sleep(0.3)
        page_params = {
            "qid": qid,
            "pageno": page,
            "responseset": page_size,
        }
        if qcolumns:
            page_params["qcolumns"] = qcolumns
        page_data = _get(f"{service}.get_qid", page_params)
        page_results = page_data.get("Results") or {}
        # SDW's get_qid returns under "WaterSystems"; CWA uses "Facilities";
        # generic ECHO uses "FacilityInfo". Try all known keys.
        rows = (page_results.get(array_key)
                or page_results.get("Facilities")
                or page_results.get("FacilityInfo")
                or page_results.get("Systems")
                or page_results.get("WaterSystems")
                or [])
        if not rows:
            break
        all_rows.extend(rows)
        page += 1

    if page > max_pages:
        log.warning("Hit max_pages=%d for QID=%s; result may be truncated.",
                    max_pages, qid)
    return all_rows


# --------------------------------------------------------- event drill-down

def fetch_npdes_violation_events(
    npdes_permit_id: str,
    start_date: str,    # MM/DD/YYYY (EPA's quirky format)
    end_date: str,
) -> list[dict]:
    """Pull every individual DMR-derived violation for one NPDES permit.

    These are the *specific* violation events your sales team cares about:
      - Which pollutant was over its limit?
      - By how much?
      - On what monitoring period?

    Each chart record contains, among other fields:
      parameter_desc          (e.g. "BOD, 5-day, 20 deg. C")
      limit_value, limit_unit
      dmr_value, dmr_unit     (what the facility actually reported)
      exceedence_pct          (how badly they blew it)
      monitoring_period_end_date
      npdes_violation_id      (stable unique ID we use for dedupe + diffs)
      violation_code          (look up in Parameter Reference Table)
    """
    params = {
        "p_id": npdes_permit_id,
        "p_start_date": start_date,
        "p_end_date": end_date,
    }
    data = _get("eff_rest_services.get_effluent_chart", params)
    chart = data.get("Results", {}).get("EffluentChart", {}) or {}

    # The chart object groups data by parameter; we flatten to per-event rows.
    events: list[dict] = []
    for param in chart.get("Parameter", []) or []:
        param_desc = param.get("ParameterDesc") or param.get("ParameterCode")
        for monitoring in param.get("MonitoringLocation", []) or []:
            for period in monitoring.get("MonitoringPeriod", []) or []:
                if str(period.get("ExceedencePct") or "").strip() in ("", "0", "0.0"):
                    continue  # not a violation, just a reading
                events.append({
                    "npdes_id": npdes_permit_id,
                    "parameter": param_desc,
                    "limit_value": period.get("LimitValue"),
                    "limit_unit": period.get("LimitUnit"),
                    "dmr_value": period.get("DMRValue"),
                    "dmr_unit": period.get("DMRUnit"),
                    "exceedance_pct": period.get("ExceedencePct"),
                    "period_end": period.get("MonitoringPeriodEndDate"),
                    "violation_code": period.get("ViolationCode"),
                    "violation_id": period.get("NPDESViolationID"),
                    "stat_basis": period.get("StatisticalBaseDesc"),
                })
    return events


def fetch_facility_dfr(registry_id: str) -> dict:
    """Detailed Facility Report - everything ECHO knows about one facility.

    For SDWA, this is how we get the individual violation list (the
    get_systems endpoint only returns aggregate counts). The DFR is also
    handy for CAA/RCRA cross-checks if you ever expand beyond water.
    """
    data = _get("dfr_rest_services.get_dfr", {"p_id": registry_id})
    return data.get("Results", {}) or {}


def fetch_sdwa_violation_events(registry_id: str) -> list[dict]:
    """Pull individual SDWA violations for one public water system.

    SDWA's per-event data isn't exposed through get_systems (which only
    returns aggregate counts). The cleanest API path is the Detailed
    Facility Report (DFR), which embeds the violation history.

    Field translation:
      - violation_code -> category + description (via sdwa_codes module).
        Categories are MCL / TreatmentTechnique / Monitoring / Reporting /
        PublicNotification - the bucket sales actually cares about.
      - contaminant_code -> human contaminant name (e.g. "Total Coliform")
      - rule_code        -> rule family name (e.g. "Lead and Copper Rule")

    Status field semantics (per EPA SDWIS):
      Unresolved - violation is still open; sales opportunity
      Addressed  - formal enforcement underway; opportunity but constrained
      Resolved   - water system returned to compliance; do NOT cold-call
      Archived   - >5 years past noncompliance end date; stale

    Defensive parsing:
      The DFR response shape varies across EPA service versions. We try
      several reasonable paths to the violations array. If none of them
      match the actual response, we log and return [] rather than crash.
    """
    # The actual SDWA violation list lives at:
    #   Results.ViolationsEnforcementActions.Sources[*].Violations
    # Each Source corresponds to a PWS attached to this registry; violations
    # are already-text dicts (FederalRule, ContaminantName, ViolationCategoryDesc,
    # Status) so we don't need the sdwa_codes lookup tables here. Older code
    # tried DrinkingWaterViolations / DFRSections[type=SDWA] — neither key
    # exists in current responses, which is why drill-down silently returned
    # zero events for every system.
    #
    # Throttle retry: EPA's DFR endpoint sometimes returns a stub response
    # (200 OK, valid JSON, but Results has only a handful of keys) when we
    # call it rapidly. A real "system has no violations" response still
    # carries the full ~40-key DFR envelope. We use key density to tell
    # them apart and retry once on suspected throttle.
    raw_violations: list[dict] = []
    results: dict = {}
    # Attempt 0 is the initial call; subsequent attempts are throttle
    # retries with progressively longer sleeps from
    # DFR_RETRY_BACKOFF_SCHEDULE. We only retry on "thin response" (the
    # throttle signal) — substantive empty responses are the truth.
    attempts = (0,) + DFR_RETRY_BACKOFF_SCHEDULE
    for attempt, backoff in enumerate(attempts):
        if backoff:
            log.debug("DFR %s: thin response previously; sleeping %ds before retry %d/%d",
                      registry_id, backoff, attempt, len(DFR_RETRY_BACKOFF_SCHEDULE))
            time.sleep(backoff)
        data = _get("dfr_rest_services.get_dfr", {"p_id": registry_id})
        results = data.get("Results", {}) or {}

        vea = results.get("ViolationsEnforcementActions") or {}
        raw_violations = []
        for source in vea.get("Sources", []) or []:
            for v in source.get("Violations", []) or []:
                if isinstance(v, dict):
                    raw_violations.append(v)

        if raw_violations:
            break
        # If response is substantive (lots of top-level keys), the empty
        # Violations list is the truth — don't waste retries. If thin
        # AND we have retries left, loop again.
        if len(results) >= DFR_THROTTLE_KEY_THRESHOLD:
            break

    if not raw_violations:
        # Distinguish "throttle persisted across all retries" (loud warning;
        # the caller should second-pass these) from "genuinely no events"
        # (debug log; expected for clean facilities).
        if len(results) < DFR_THROTTLE_KEY_THRESHOLD:
            log.warning("DFR %s: throttle persisted across all retries — "
                        "no events drilled. Consider second-pass.",
                        registry_id)
        else:
            log.debug("No SDWA violations found in DFR for %s (Results has %d keys)",
                      registry_id, len(results))
        return []

    events: list[dict] = []
    for v in raw_violations:
        events.append({
            "violation_id": v.get("ViolationID"),
            "registry_id": registry_id,
            "program": "SDWA",
            "source_id": v.get("SourceID"),
            "violation_code": v.get("ViolationCategoryCode"),
            "violation_category": v.get("ViolationCategoryDesc"),
            "violation_description": v.get("FederalRule"),
            "contaminant": v.get("ContaminantName"),
            "rule_family": v.get("FederalRule"),
            "period_begin": v.get("NonCompliancePeriodBeginDate")
                            or v.get("CompliancePeriodBeginDate"),
            "period_end":   v.get("NonCompliancePeriodEndDate")
                            or v.get("CompliancePeriodEndDate"),
            "resolved_date": v.get("ResolvedDate"),
            "status":       v.get("Status"),
            "state_mcl":    v.get("StateMCL"),
            "federal_mcl":  v.get("FederalMCL"),
            "measure":      v.get("ViolationMeasure"),
            "enforcement_count": len(v.get("EnforcementActions") or []),
        })
    return events


# --------------------------------------------------------- convenience

def iter_violators_by_states(
    states: Iterable[str],
    naics_prefixes: Iterable[str],
):
    """Generator: yields (program, raw_record) tuples across a territory.
    Keeps the main pipeline readable - one for-loop instead of nested ones."""
    for st in states:
        for naics in naics_prefixes:
            for fac in find_cwa_violators(st, naics):
                yield "CWA", fac
        for sys_ in find_sdwa_violators(st):
            yield "SDWA", sys_