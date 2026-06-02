"""
bulk_loader.py
==============
Nationwide-scale data pull using EPA's published bulk CSVs.

For nationwide queries, the per-state / per-NAICS API loop in pipeline.py
takes hours. EPA publishes the *same data* as weekly bulk downloads —
one zip with summary records for every regulated facility in the country.
Downloading and stream-filtering takes 5–15 minutes total.

Files we pull:

  echo_exporter.zip             ECHO Exporter — one row per facility
                                (~1.5M rows, ~130 columns, weekly refresh)
                                The replacement for find_*_violators().

  npdes_downloads.zip (optional)  Individual NPDES violation events
                                  (NPDES_VIOLATIONS.csv inside).

  SDWA_latest_downloads.zip       Individual SDWA violations
  (optional)                      (SDWA_VIOLATIONS_ENFORCEMENT.csv inside).
                                  Refreshed quarterly.

URL CONSTANTS BELOW: EPA occasionally renames bulk files. If a download
fails, verify the current name at
    https://echo.epa.gov/tools/data-downloads
and update the BULK_URLS dict.

Output shape is identical to pipeline.py — all_leads.csv, violation_events.csv,
new_*.csv. Snapshot/diff logic and lag-warning surfaces are reused unchanged,
so a sales team using the API pipeline can switch to bulk without learning
new CSV columns.

Why streaming, not pandas:
  The ECHO Exporter CSV is large (~250MB unzipped). Loading it into
  pandas takes ~2GB of RAM. Stream-parsing with csv.DictReader and
  filtering as we go keeps memory under 100MB and runs in similar time.
  It also means this module has zero non-stdlib dependencies.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
import requests

from . import _health, scoring, snapshot
from .pipeline import (
    TARGET_NAICS, LAG_BANNER, SDWA_LAG_NOTE, CWA_LAG_NOTE,
    EVENT_DRILLDOWN_MIN_SCORE, LOOKBACK_DAYS,
    _drill_cwa, _drill_sdwa, _write_csv, _write_lag_notice, _run_output_dir,
)

log = logging.getLogger("chemtreat.bulk")


# ----------------------------------------------------- URLs (VERIFY THESE)

BULK_URLS = {
    # ECHO Exporter — facility summary, weekly refresh, ~250 MB unzipped
    "echo_exporter": "https://echo.epa.gov/files/echodownloads/echo_exporter.zip",
    # ICIS-NPDES national data — individual violation events
    "npdes":         "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip",
    # SDWA datasets — quarterly refresh
    "sdwa":          "https://echo.epa.gov/files/echodownloads/SDWA_latest_downloads.zip",
    # NPDES permit limits — pre-violation signal: what each facility is
    # permitted to discharge. ~513 MB compressed, 7.2 GB unzipped, weekly.
    # Stream-filtered against kept_npdes_permits so the unzipped size
    # never lands in memory.
    "npdes_limits":  "https://echo.epa.gov/files/echodownloads/npdes_limits.zip",
    # NPDES-ATTAINS catchment linkage — pre-spatially-joined assignment
    # of each NPDES outfall to downstream assessed waters with
    # impairment status. ~100 MB compressed, weekly.
    "npdes_attains": "https://echo.epa.gov/files/echodownloads/npdes_attains_downloads.zip",
}

CACHE_MAX_AGE_DAYS = 7   # ECHO refreshes weekly; cache for that long


# ----------------------------------------------------- download w/ cache

def _download_cached(url: str, cache_dir: Path, name: str) -> Path:
    """Download `url` to `cache_dir/name.zip`. Skip if cached file is fresh.

    Caching here is important: the ECHO Exporter is ~250 MB. You don't want
    to pull it on every run. EPA refreshes weekly, so a 7-day cache window
    matches their update cadence.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{name}.zip"

    if target.exists():
        age = datetime.utcnow() - datetime.utcfromtimestamp(target.stat().st_mtime)
        if age < timedelta(days=CACHE_MAX_AGE_DAYS):
            log.info("Using cached %s (%.1f days old, %.1f MB)",
                     name, age.total_seconds() / 86400,
                     target.stat().st_size / 1e6)
            return target
        log.info("Cached %s is %.1f days old (>%d); re-downloading",
                 name, age.total_seconds() / 86400, CACHE_MAX_AGE_DAYS)

    log.info("Downloading %s from %s …", name, url)
    # Use `requests` (which trusts certifi's CA bundle) rather than
    # urllib.request.urlretrieve — the latter relies on the system's
    # OpenSSL trust store which is empty on stock macOS Python.framework
    # installs and trips SSL: CERTIFICATE_VERIFY_FAILED.
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with target.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    log.info("Saved %s (%.1f MB)", name, target.stat().st_size / 1e6)
    return target


# ----------------------------------------------------- ECHO Exporter

def _safe_int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _row_matches_naics(row: dict, prefixes: list[str]) -> bool:
    """The ECHO Exporter packs multiple NAICS codes into one space- or
    comma-separated string. We do prefix matching to catch sub-codes."""
    naics_str = row.get("FAC_NAICS_CODES") or ""
    # Codes may be separated by spaces, commas, or pipes
    for separator in (" ", ",", "|"):
        naics_str = naics_str.replace(separator, " ")
    codes = [c.strip() for c in naics_str.split() if c.strip()]
    return any(c.startswith(p) for c in codes for p in prefixes)


# --------------------------------------------------------- program signals
#
# Two program-specific predicates instead of one combined gate. CWA and
# SDWA cannot share a "has any signal" check because the scoring rules
# read program-specific field names — and Python's `or` collapses CWA's
# string "0" onto SDWA's "5" silently, masking the SDWA signal. Splitting
# the discovery gate and the raw-shape dict eliminates the mask.

_SNC_CLEAN_STATUSES = {"NO VIOLATION IDENTIFIED", "NOT APPLICABLE", "UNKNOWN", "N/A", ""}


def _row_has_cwa_signal(row: dict) -> bool:
    """True if the facility has any CWA compliance signal worth attention."""
    if str(row.get("CWA_SNC_FLAG") or "").upper() in ("Y", "S"):
        return True
    if _safe_int(row.get("CWA_FORMAL_ACTION_COUNT")) > 0:
        return True
    if _safe_int(row.get("CWA_QTRS_WITH_NC")) > 0:
        return True
    if str(row.get("CWA_CURRENT_VIOL") or "").upper() == "Y":
        return True
    status = str(row.get("CWA_COMPLIANCE_STATUS") or "").strip().upper()
    if status and status not in _SNC_CLEAN_STATUSES:
        return True
    return False


def _row_has_sdwa_signal(row: dict) -> bool:
    """True if the facility has any SDWA compliance signal worth attention.

    The ECHO Exporter exposes only four SDWA facility-level signals:
    SDWA_SNC_FLAG, SDWA_FORMAL_ACTION_COUNT, SDWA_INFORMAL_COUNT, and
    SDWA_COMPLIANCE_STATUS (verbose text). No quarters-with-vio or
    Pb/Cu flags at this level — chronic / lead-copper rules need event
    data (from the SDWA bulk events file or API fallback) to fire for
    SDWA. Documented in RATIONALE.md.

    Discovery gate intentionally NARROW: SNC flag OR formal-action
    count. We deliberately do NOT accept generic SDWA_COMPLIANCE_STATUS
    text — empirically (TX, 167k rows), "Inactive" and "Violation
    Identified" together account for ~10K rows that aren't actionable
    leads (Inactive = not operating, Violation Identified = generic
    catch-all). "Enforcement Priority" — the only status text the
    scorer's text-match recognizes — is perfectly correlated with
    SNC_FLAG=Y in practice, so the narrow gate doesn't lose signal.
    """
    if str(row.get("SDWA_SNC_FLAG") or "").upper() in ("Y", "S"):
        return True
    if _safe_int(row.get("SDWA_FORMAL_ACTION_COUNT")) > 0:
        return True
    return False


def _bulk_to_program_shapes(row: dict) -> list[tuple[str, dict]]:
    """Build per-program raw dicts for the scorer.

    Returns 0, 1, or 2 (program, raw_dict) tuples. CWA-only when only
    CWA signals fire, SDWA-only when only SDWA signals fire, both when
    both fire, empty otherwise.

    **Each raw dict carries ONLY that program's keys** — no cross-program
    aliases. This is load-bearing: the scoring rules use Python `or`
    fallbacks (`f.get("CWPFormalEaCnt") or f.get("Feas")`), and a CWA
    `"0"` string is truthy enough to mask the SDWA value. Two clean
    dicts make that impossible.

    Key names match what `scoring.py` reads (verified against
    rule_significant_violator, rule_chronic_violation, rule_formal_action,
    rule_major_facility, rule_recent_penalty, rule_recent_inspection,
    compute_tags).
    """
    out: list[tuple[str, dict]] = []

    identity = {
        "FacName": row.get("FAC_NAME"),
        "FacStreet": row.get("FAC_STREET"),
        "FacCity": row.get("FAC_CITY"),
        "FacState": row.get("FAC_STATE"),
        "FacZip": row.get("FAC_ZIP"),
        "FacCounty": row.get("FAC_COUNTY"),
        "FacNAICSCodes": row.get("FAC_NAICS_CODES"),
        "FacSICCodes": row.get("FAC_SIC_CODES"),
        "RegistryID": row.get("REGISTRY_ID"),
    }

    if _row_has_cwa_signal(row):
        cwa_status = str(row.get("CWA_COMPLIANCE_STATUS") or "").strip() or None
        cwa = {
            **identity,
            "SourceID": row.get("NPDES_IDS"),
            "SNCFlag": "Y" if str(row.get("CWA_SNC_FLAG") or "").upper()
                              in ("Y", "S") else "N",
            "CWPSNCStatus": cwa_status,
            "CWPQtrsWithNC": row.get("CWA_QTRS_WITH_NC"),
            "CWPFormalEaCnt": row.get("CWA_FORMAL_ACTION_COUNT"),
            "CWPInformalEnfActCount": row.get("CWA_INFORMAL_COUNT"),
            "CWPPermitTypes": row.get("CWA_PERMIT_TYPES"),
            "CWPTotalPenalties": row.get("CWA_LAST_PENALTY_AMT"),
            "CWPDaysLastInspection": row.get("CWA_DAYS_LAST_INSPECTION"),
            "CWP13qtrsComplHistory": row.get("CWA_13QTRS_COMPL_HISTORY"),
            "CWPViolStatus": row.get("CWA_CURRENT_VIOL"),
        }
        out.append(("CWA", cwa))

    if _row_has_sdwa_signal(row):
        sdwa_status = str(row.get("SDWA_COMPLIANCE_STATUS") or "").strip() or None
        snc_y = str(row.get("SDWA_SNC_FLAG") or "").upper() in ("Y", "S")
        sdwa = {
            **identity,
            "SourceID": row.get("SDWA_IDS"),
            "SNCFlag": "Y" if snc_y else "N",
            "SNC": sdwa_status,
            "SeriousViolator": "Y" if snc_y else "N",
            "Feas": row.get("SDWA_FORMAL_ACTION_COUNT"),
            "Ifea": row.get("SDWA_INFORMAL_COUNT"),
        }
        out.append(("SDWA", sdwa))

    return out


def stream_echo_exporter(zip_path: Path,
                         naics_prefixes: list[str],
                         states: list[str] | None = None):
    """Generator yielding filtered (raw_row, program, prog_raw) triples.

    One row in the ECHO Exporter can produce zero, one, or two emissions:
    CWA-only, SDWA-only, or both. Each emission carries a program-specific
    raw dict (see `_bulk_to_program_shapes`).

    Gating rules differ by program:
      - CWA: must match TARGET_NAICS server-side (this is what ChemTreat
        sells to — industrial dischargers).
      - SDWA: emit regardless of NAICS. Public water systems are the
        customer; they don't carry the industrial NAICS classification
        the exporter uses for facilities. Mirrors `find_sdwa_violators`
        in the API path, which applies no NAICS filter.

    If `states` is provided, only rows from those state codes are yielded.
    Stream-processed so we don't load 1.5M rows into RAM.
    """
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(f"No CSV inside {zip_path}")
        # ECHO Exporter zips contain a single big CSV. If there are multiple
        # (rare), the main one is the largest by name pattern.
        main_csv = sorted(csv_names, key=lambda n: -len(n))[0]
        log.info("Reading %s from %s", main_csv, zip_path.name)

        with zf.open(main_csv) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace")
            reader = csv.DictReader(text_fh)
            total = matched = 0
            for row in reader:
                total += 1
                if states and str(row.get("FAC_STATE") or "").upper() not in states:
                    continue
                shapes = _bulk_to_program_shapes(row)
                if not shapes:
                    continue
                naics_ok = _row_matches_naics(row, naics_prefixes)
                emitted_any = False
                for program, prog_raw in shapes:
                    if program == "CWA" and not naics_ok:
                        continue
                    yield row, program, prog_raw
                    emitted_any = True
                if emitted_any:
                    matched += 1
                if total % 100_000 == 0:
                    log.info("  scanned %d rows, %d matches so far",
                             total, matched)
            log.info("Scanned %d total rows, %d matched filters", total, matched)


# ----------------------------------------------------- event drill-down
#
# For nationwide bulk, we read individual events from the NPDES violations
# CSV and the SDWA violations CSV rather than calling the API per facility.
# Both are streaming-safe.

# --------------------------------------------------------- event shape
#
# Bulk event CSVs use different field names and a different category
# vocabulary than the API DFR drill-down. Without normalization, the
# scoring.EVENT_RULES (which key on substrings like "TREATMENT TECHNIQUE"
# in violation_category) never fire on bulk events — so bulk-loaded
# facilities get facility-only scores and never populate outreach_posture
# / tag_* via the phase-2 augmentation.
#
# These helpers map bulk shape onto API shape (the canonical schema that
# scoring + viewer expect). The translation is conservative: when bulk
# doesn't carry a field the API path has (e.g. enforcement_count), we
# pass through whatever bulk *did* have so the row still flows.

# Short-form -> API-shape category names. Bulk uses sdwa_codes.lookup_violation
# which returns curt labels ("MCL", "TreatmentTechnique"); the API uses
# EPA's verbose strings. EVENT_RULES substring-matches on the verbose form.
_SDWA_CATEGORY_EXPANSION = {
    "MCL":                 "Maximum Contaminant Level Violation",
    "MRDL":                "Maximum Residual Disinfectant Level Violation",
    "TreatmentTechnique":  "Treatment Technique Violation",
    "Monitoring":          "Monitoring and Reporting",
    "Reporting":           "Monitoring and Reporting",
    "PublicNotification":  "Public Notification Rule Violation",
}

# Status codes seen in SDWIS bulk → API's outreach vocabulary. SDWIS
# uses single-character codes (R/U/A/K) AND fuller strings depending on
# the field; we accept both.
_SDWA_STATUS_NORMALIZATION = {
    "U": "Unaddressed",     "UNADDRESSED": "Unaddressed",   "OPEN": "Unaddressed",
    "UNRESOLVED": "Unaddressed",
    "A": "Addressed",       "ADDRESSED": "Addressed",
    "RTC": "Resolved",      "RESOLVED": "Resolved",
    "RETURNED TO COMPLIANCE": "Resolved",
    "K": "Archived",        "ARCHIVED": "Archived",
}


def _normalize_bulk_sdwa_event(e: dict) -> dict:
    """Reshape a bulk SDWA event into the API event schema."""
    cat = e.get("violation_category") or ""
    e["violation_category"] = _SDWA_CATEGORY_EXPANSION.get(cat, cat)
    status_raw = str(e.get("status") or "").strip().upper()
    e["status"] = _SDWA_STATUS_NORMALIZATION.get(status_raw, e.get("status") or "Unaddressed")
    # Field the API path supplies that bulk doesn't.
    e.setdefault("source_id", e.pop("pwsid", None))
    e.setdefault("resolved_date", e.get("period_end") if e["status"] == "Resolved" else None)
    e.setdefault("enforcement_count", 0)
    e.setdefault("state_mcl", None)
    e.setdefault("federal_mcl", None)
    e.setdefault("measure", None)
    return e


def _normalize_bulk_npdes_event(e: dict) -> dict:
    """Reshape a bulk NPDES event into the API event schema.

    Bulk NPDES doesn't carry per-event status the same way API does;
    we default to Unaddressed for any row that isn't explicitly closed.
    """
    rnc = str(e.get("status") or "").strip().upper()
    # RNC_DETECTION_CODE values: blank/null = open, specific codes can
    # indicate resolved. Without a documented enum we default to open
    # unless we see an explicit clearance marker. Sales should verify on
    # ECHO anyway (the data-lag warning makes that the standing advice).
    if rnc in ("", "0", "NONE"):
        e["status"] = "Unaddressed"
    elif rnc in _SDWA_STATUS_NORMALIZATION:
        e["status"] = _SDWA_STATUS_NORMALIZATION[rnc]
    else:
        e["status"] = "Unaddressed"
    # Map permit_id -> npdes_id (the schema column name).
    if e.get("permit_id"):
        e.setdefault("npdes_id", e.pop("permit_id"))
    return e


# NPDES bulk violation CSVs we actually want to read. The previous
# selector picked NPDES_VIOLATION_ENFORCEMENTS.csv, which is a join
# table (violations ↔ enforcement actions) with no NPDES_ID column —
# wrong file. The substantive per-event data lives in three sibling
# files: Single-Event (effluent exceedances), Permit-Schedule
# (compliance milestones), Compliance-Schedule (long-form schedules).
# All three share NPDES_ID, NPDES_VIOLATION_ID, VIOLATION_CODE,
# VIOLATION_DESC, RNC_DETECTION_CODE, and date columns.
_NPDES_VIOLATION_FILES = (
    "NPDES_SE_VIOLATIONS.csv",
    "NPDES_PS_VIOLATIONS.csv",
    "NPDES_CS_VIOLATIONS.csv",
)


def stream_npdes_violations(zip_path: Path,
                            registry_id_set: set[str],
                            permit_id_set: set[str] | None = None,
                            permit_to_registry: dict[str, str] | None = None
                            ) -> list[dict]:
    """Pull NPDES violation events for the facilities we kept.

    Joins by REGISTRY_ID when present on the bulk row, otherwise by
    NPDES_ID. Bulk NPDES violation CSVs do NOT carry REGISTRY_ID in
    practice (verified against NPDES_SE_VIOLATIONS / NPDES_PS_VIOLATIONS
    / NPDES_CS_VIOLATIONS headers), so the permit-id fallback is
    actually the dominant path. `permit_to_registry` lets us backfill
    the lead's RegistryID onto the event so the downstream
    `events_by_key` join works and `snapshot`'s `registry_id` column
    is populated.

    Reads SE + PS + CS files (effluent exceedances + permit-schedule
    milestones + compliance-schedule events). All three share the same
    column shape for our purposes.
    """
    permit_id_set = permit_id_set or set()
    permit_to_registry = permit_to_registry or {}
    events: list[dict] = []
    with zipfile.ZipFile(zip_path) as zf:
        present = {n for n in zf.namelist()}
        targets = [n for n in _NPDES_VIOLATION_FILES if n in present]
        if not targets:
            log.warning("No NPDES per-event violation CSV found inside %s "
                        "(looked for %s)",
                        zip_path.name, ", ".join(_NPDES_VIOLATION_FILES))
            return events
        for target in targets:
            log.info("Reading %s", target)
            with zf.open(target) as raw_fh:
                text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                          errors="replace")
                for row in csv.DictReader(text_fh):
                    row_reg = row.get("REGISTRY_ID") or None
                    permit = (row.get("NPDES_ID")
                              or row.get("EXTERNAL_PERMIT_NMBR"))
                    # Apply the join filter early to keep memory bounded.
                    keep = False
                    backfill_reg = None
                    if row_reg and row_reg in registry_id_set:
                        keep = True
                    elif permit and permit in permit_id_set:
                        keep = True
                        backfill_reg = permit_to_registry.get(permit)
                    if not keep:
                        continue
                    events.append({
                        "violation_id": row.get("NPDES_VIOLATION_ID")
                                        or row.get("VIOLATION_ID"),
                        "registry_id": row_reg or backfill_reg,
                        "permit_id": permit,
                        "program": "CWA",
                        "violation_code": row.get("VIOLATION_CODE"),
                        "violation_description": row.get("VIOLATION_DESC"),
                        # Bulk NPDES violation files don't carry the per-DMR
                        # parameter / limit_value / dmr_value / exceedance_pct
                        # columns the API path (`get_effluent_chart`) does;
                        # set them to None so the schema slots stay populated.
                        "parameter": None,
                        "limit_value": None,
                        "dmr_value": None,
                        "exceedance_pct": None,
                        "period_end": row.get("SINGLE_EVENT_END_DATE")
                                       or row.get("SCHEDULE_DATE")
                                       or row.get("RNC_DETECTION_DATE"),
                        "resolved_date": row.get("RNC_RESOLUTION_DATE")
                                          or row.get("REPORT_RECEIVED_DATE"),
                        "status": row.get("RNC_DETECTION_CODE") or "Unresolved",
                        "data_lag_note": CWA_LAG_NOTE,
                    })
    log.info("Kept %d NPDES violation events after join", len(events))
    return [_normalize_bulk_npdes_event(e) for e in events]


def stream_sdwa_violations(zip_path: Path,
                           registry_id_set: set[str],
                           pwsid_set: set[str] | None = None,
                           pwsid_to_registry: dict[str, str] | None = None
                           ) -> list[dict]:
    """Pull SDWA violation events for kept facilities.

    Joins by REGISTRY_ID when present, otherwise by PWSID. The SDWA
    bulk does NOT carry REGISTRY_ID at all on violation rows — only
    PWSID — so the PWSID fallback is the only working join path. We
    backfill the lead's RegistryID onto the event via `pwsid_to_registry`
    so snapshot's `registry_id` column is populated.
    """
    from . import sdwa_codes
    pwsid_set = pwsid_set or set()
    pwsid_to_registry = pwsid_to_registry or {}
    events: list[dict] = []

    with zipfile.ZipFile(zip_path) as zf:
        target = next(
            (n for n in zf.namelist()
             if "VIOLATION" in n.upper() and n.lower().endswith(".csv")),
            None,
        )
        if target is None:
            log.warning("No SDWA violations CSV found inside %s", zip_path.name)
            return events
        log.info("Reading %s", target)
        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace")
            for row in csv.DictReader(text_fh):
                row_reg = row.get("REGISTRY_ID") or None
                pwsid = row.get("PWSID")
                keep = False
                backfill_reg = None
                if row_reg and row_reg in registry_id_set:
                    keep = True
                elif pwsid and pwsid in pwsid_set:
                    keep = True
                    backfill_reg = pwsid_to_registry.get(pwsid)
                if not keep:
                    continue
                vio_code = str(row.get("VIOLATION_CODE") or "")
                cont_code = str(row.get("CONTAMINANT_CODE") or "")
                rule_code = str(row.get("RULE_CODE") or row.get("RULE_FAMILY_CODE") or "")
                category, vio_desc = sdwa_codes.lookup_violation(vio_code)
                events.append({
                    "violation_id": row.get("VIOLATION_ID"),
                    "registry_id": row_reg or backfill_reg,
                    "pwsid": pwsid,
                    "program": "SDWA",
                    "violation_code": vio_code,
                    "violation_category": category,
                    "violation_description": vio_desc,
                    "contaminant_code": cont_code,
                    "contaminant": sdwa_codes.lookup_contaminant(cont_code),
                    "rule_family": sdwa_codes.lookup_rule(rule_code),
                    "period_begin": row.get("NON_COMPL_PER_BEGIN_DATE"),
                    "period_end": row.get("NON_COMPL_PER_END_DATE"),
                    "status": row.get("VIOLATION_STATUS"),
                    "is_health_based": row.get("IS_HEALTH_BASED_IND"),
                    "pn_tier": row.get("CALCULATED_PUB_NOTIFICATION_TIER"),
                    "data_lag_note": SDWA_LAG_NOTE,
                })
    log.info("Kept %d SDWA violation events after join", len(events))
    return [_normalize_bulk_sdwa_event(e) for e in events]


# ----------------------------------------------------- pre-violation signals
#
# Two new bulk feeds, both NPDES-side, both rolled up to a single dict
# per join key so the augmentation step in run_bulk can `lead.update(sig)`.
# See EXTERNAL_DATA_PLAN.md for the design and EXTERNAL_DATA_STATUS.md
# for what other feeds are queued behind these.

# Substring patterns for ChemTreat-treatable parameter classes. Matched
# against PARAMETER_DESC (already upper-cased before comparison). Pinned
# against empirical EPA wording verified from a fresh npdes_limits.zip
# pull — see EXTERNAL_DATA_PLAN.md for the dump that informed each
# entry. Adding a class here also needs a column in snapshot.py
# (`permit_has_<class>`) and a corresponding entry in
# `scoring.PERMIT_HAS_COLS`.
_TREATABLE_PARAM_PATTERNS: dict[str, tuple[str, ...]] = {
    "phosphorus":       ("PHOSPHORUS",),
    "ammonia":          ("AMMONIA",),
    # EPA renders TSS as "Solids, total suspended"; the "TSS" literal is
    # rare but appears in older permits — keep both.
    "tss":              ("TOTAL SUSPENDED", "TSS"),
    # BOD has many wordings ("BOD, 5-day, 20 deg. C",
    # "BOD, carbonaceous [5 day, 20 C]", etc); the "BOD" substring catches
    # them all. "BIOCHEMICAL OXYGEN" is a backstop for rare spell-outs.
    "bod":              ("BOD", "BIOCHEMICAL OXYGEN"),
    # Both "Oil & Grease" and "Oil and grease" appear in the wild —
    # different permit-writers prefer different forms.
    "oil_grease":       ("OIL AND GREASE", "OIL & GREASE"),
    "metals":           ("COPPER", "LEAD,", "ZINC", "NICKEL",
                         "CHROMIUM", "CADMIUM"),
    # "LEAD," (with the comma) avoids matching "LEADING" or
    # "LEAD-COPPER" composite labels. EPA's metals parameters are always
    # of the form "Lead, total recoverable" / "Lead, total dissolved".
    "chlorine_residual": ("CHLORINE, TOTAL RESIDUAL",
                          "TOTAL RESIDUAL CHLORINE"),
}


def _classify_parameter(param_desc: str) -> str | None:
    """Return the ChemTreat-treatable class for a PARAMETER_DESC, or None.

    Pure helper kept module-level so tests can assert pattern
    coverage without owning the streamer's IO."""
    up = param_desc.upper()
    for cls, patterns in _TREATABLE_PARAM_PATTERNS.items():
        if any(p in up for p in patterns):
            return cls
    return None


# Cap on distinct PARAMETER_DESC values stored in
# permitted_parameters_text. A typical permit has 5–15 parameters; some
# major refineries push 40+. The viewer renders this as a single cell —
# anything past ~15 just stretches the row and hides the SNC summary.
_MAX_PERMITTED_PARAMS_SAMPLED = 15


def stream_permit_limits(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Build per-NPDES-permit signal dicts from npdes_limits.zip.

    Returns ``{npdes_id: {permit_has_*: 1, permitted_parameters_text: "..."}}``.
    Only permits in `kept_npdes_permits` are kept — the file is 7+ GB
    unzipped and an unfiltered scan would balloon memory.

    Filters:
      * `EXTERNAL_PERMIT_NMBR in kept_npdes_permits` (the NPDES_ID).
        The bulk export's `kept_npdes_permits` set is the same one
        passed to `stream_npdes_violations`, so the join shape matches.
      * `LIMIT_SET_STATUS_FLAG == 'A'` (active). Inactive limit-sets
        carry expired permit revisions and would produce false positive
        signals — empirically ~5% of rows are 'I'.

    Per-permit rollup:
      * Each `permit_has_<class>` flag is 1 iff at least one active
        limit-set row on that permit has a PARAMETER_DESC matching the
        class. Detail per-outfall / per-statistic-base is not preserved.
      * `permitted_parameters_text` collects up to
        ``_MAX_PERMITTED_PARAMS_SAMPLED`` distinct treatable parameter
        descriptions, pipe-joined, alphabetized for stable diffs.
        Non-treatable parameters (pH, Flow, temperature, etc.) are
        intentionally NOT included — the goal is sales-call material,
        not a permit dump.
    """
    if not kept_npdes_permits:
        log.info("No CWA permits in scope; skipping permit-limits scan.")
        return {}

    out: dict[str, dict] = {}
    seen_params: dict[str, set[str]] = {}
    rows_scanned = rows_matched = 0

    with zipfile.ZipFile(zip_path) as zf:
        # The file is a single big CSV; the existing exporter selector
        # (longest name in zip) would pick something wrong if EPA ever
        # adds a sidecar — pin by suffix instead.
        target = next(
            (n for n in zf.namelist() if n.upper().endswith("NPDES_LIMITS.CSV")),
            None,
        )
        if target is None:
            raise RuntimeError(
                f"No NPDES_LIMITS.csv found inside {zip_path.name}; "
                "EPA may have renamed the file — check "
                "https://echo.epa.gov/tools/data-downloads"
            )
        log.info("Reading %s (filter: %d permit IDs in scope)",
                 target, len(kept_npdes_permits))

        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                rows_scanned += 1
                permit = row.get("EXTERNAL_PERMIT_NMBR")
                if not permit or permit not in kept_npdes_permits:
                    continue
                if (row.get("LIMIT_SET_STATUS_FLAG") or "").strip().upper() != "A":
                    continue
                param_desc = (row.get("PARAMETER_DESC") or "").strip()
                if not param_desc:
                    continue
                cls = _classify_parameter(param_desc)
                if cls is None:
                    continue
                rows_matched += 1

                sig = out.setdefault(permit, {})
                sig[f"permit_has_{cls}"] = 1
                params = seen_params.setdefault(permit, set())
                if len(params) < _MAX_PERMITTED_PARAMS_SAMPLED:
                    params.add(param_desc)

                if rows_scanned % 1_000_000 == 0:
                    log.info("  permit-limits scanned %d rows, %d matched",
                             rows_scanned, rows_matched)

    for permit, params in seen_params.items():
        out[permit]["permitted_parameters_text"] = " | ".join(sorted(params))
    log.info("Permit-limits done: scanned %d rows, kept signal for %d permits",
             rows_scanned, len(out))
    return out


# WATER_CONDITION values observed in NPDES_ATTAINS_AU_SUMMARIES.csv
# (sampled 500k rows from a real 2026-05-30 pull). Anything starting
# "Impaired" counts as impaired regardless of restoration-plan status.
# "Good", "Unknown", "Good - With Restoration Plan", and
# "Unknown - With Restoration Plan" do NOT.
def _is_impaired_condition(condition: str) -> bool:
    return condition.strip().upper().startswith("IMPAIRED")


def stream_attains_linkage(
    zip_path: Path,
    kept_registry_ids: set[str],
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Build per-RegistryID ATTAINS signals from
    npdes_attains_downloads.zip.

    Returns ``{registry_id: {discharges_to_impaired: 1,
                             impairment_causes_text: "...",
                             matching_impaired_parameters: "..."}}``.

    The summary file (`NPDES_ATTAINS_AU_SUMMARIES.csv`) carries one row
    per (facility, assessment unit) — a single permit that drains into
    three impaired units shows up three times. We roll up by REGISTRY_ID
    because that's the lead-row key.

    Join keys (OR'd):
      * REGISTRY_ID in `kept_registry_ids` (almost always present).
      * NPDES_ID in `kept_npdes_permits` (fallback — same defensive
        pattern as `stream_npdes_violations`).

    Signal extraction:
      * `discharges_to_impaired`: any row's WATER_CONDITION starts with
        "Impaired" (matches "Impaired", "Impaired - 303(d) Listed",
        "Impaired - With Restoration Plan", etc.).
      * `impairment_causes_text`: pipe-joined union of
        CAUSE_GROUPS_IMPAIRED values across all rows. Sorted for stable
        snapshot diffs.
      * `matching_impaired_parameters`: pipe-joined union of
        E90_POT_IMP_PARAMETERS — the facility's MONITORED effluent
        parameters that the state has identified as causes of the
        waterbody's impairment. Rare (~1% of rows in the sample) but
        every match is a high-confidence permit-tightening lead.
    """
    if not kept_registry_ids and not kept_npdes_permits:
        log.info("No registry IDs or permits in scope; skipping ATTAINS scan.")
        return {}

    out: dict[str, dict] = {}
    causes_by_reg: dict[str, set[str]] = {}
    params_by_reg: dict[str, set[str]] = {}
    rows_scanned = rows_matched = 0

    with zipfile.ZipFile(zip_path) as zf:
        target = next(
            (n for n in zf.namelist()
             if n.upper().endswith("NPDES_ATTAINS_AU_SUMMARIES.CSV")),
            None,
        )
        if target is None:
            raise RuntimeError(
                f"No NPDES_ATTAINS_AU_SUMMARIES.csv inside {zip_path.name}; "
                "EPA may have renamed the file — check "
                "https://echo.epa.gov/tools/data-downloads"
            )
        log.info("Reading %s (filter: %d registry IDs, %d permits in scope)",
                 target, len(kept_registry_ids), len(kept_npdes_permits))

        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                rows_scanned += 1
                reg = row.get("REGISTRY_ID")
                # Prefer registry-id join; fall back to NPDES_ID for the
                # rare row where REGISTRY_ID is blank. Keep the resolved
                # registry-id-or-None as the rollup key — without one
                # we can't attach the signal to a lead.
                if not reg or reg not in kept_registry_ids:
                    permit = row.get("NPDES_ID")
                    if not permit or permit not in kept_npdes_permits:
                        continue
                    # Unmatched registry_id but matched permit: we
                    # still don't know the lead's registry_id from
                    # here. Skip — bulk_loader's permit_to_registry
                    # backfill is a separate concern (it backfills
                    # event rows, not facility signals). In practice
                    # 99%+ of NPDES_ATTAINS rows carry REGISTRY_ID so
                    # this is a no-op edge case.
                    continue
                rows_matched += 1

                sig = out.setdefault(reg, {})
                condition = row.get("WATER_CONDITION") or ""
                if _is_impaired_condition(condition):
                    sig["discharges_to_impaired"] = 1
                causes = (row.get("CAUSE_GROUPS_IMPAIRED") or "").strip()
                if causes:
                    bucket = causes_by_reg.setdefault(reg, set())
                    for piece in causes.split("|"):
                        piece = piece.strip()
                        if piece:
                            bucket.add(piece)
                e90 = (row.get("E90_POT_IMP_PARAMETERS") or "").strip()
                if e90:
                    bucket = params_by_reg.setdefault(reg, set())
                    for piece in e90.split("|"):
                        piece = piece.strip()
                        if piece:
                            bucket.add(piece)

                if rows_scanned % 500_000 == 0:
                    log.info("  ATTAINS scanned %d rows, %d matched",
                             rows_scanned, rows_matched)

    for reg, causes in causes_by_reg.items():
        out[reg]["impairment_causes_text"] = " | ".join(sorted(causes))
    for reg, params in params_by_reg.items():
        out[reg]["matching_impaired_parameters"] = " | ".join(sorted(params))
    log.info("ATTAINS done: scanned %d rows, kept signal for %d facilities",
             rows_scanned, len(out))
    return out


# ----------------------------------------------------- main pipeline

def _load_prior_scores(db_path: Path) -> dict[tuple[str, str], int]:
    """Snapshot lead scores from the DB before this run upserts.

    Returns `{(registry_id, program): lead_score}`. Used to identify
    newly-discovered facilities and facilities whose score has jumped,
    both of which qualify for the API fine-comb drill-down even when
    they're below `EVENT_DRILLDOWN_MIN_SCORE`. Opens its own DB context
    so it doesn't interfere with the run's main persistence block.
    """
    prior: dict[tuple[str, str], int] = {}
    with snapshot.open_db(db_path) as conn:
        for r in conn.execute(
            "SELECT registry_id, program, lead_score FROM facilities"
        ):
            if r["registry_id"]:
                prior[(r["registry_id"], r["program"])] = r["lead_score"] or 0
    return prior


def _build_lead_row(prog_raw: dict, program: str,
                    score: int, reasons: list[str]) -> dict:
    """Construct the lead dict for a single (facility, program) pair.

    Program-aware: pulls from CWA-named or SDWA-named keys depending on
    which raw dict was handed in. Tag columns are initialised to False
    here and overwritten by `compute_tags` after the phase-2 event
    augmentation.
    """
    reg_id = prog_raw.get("RegistryID")
    if program == "CWA":
        snc_status = prog_raw.get("CWPSNCStatus")
        violation_status = prog_raw.get("CWPViolStatus")
        quarters = prog_raw.get("CWPQtrsWithNC")
        formal = prog_raw.get("CWPFormalEaCnt")
        informal = prog_raw.get("CWPInformalEnfActCount")
        penalties = prog_raw.get("CWPTotalPenalties")
        inspection_days = prog_raw.get("CWPDaysLastInspection")
        compliance_history = prog_raw.get("CWP13qtrsComplHistory")
    else:
        snc_status = prog_raw.get("SNC")
        violation_status = ("Yes" if str(prog_raw.get("SNCFlag") or "").upper() == "Y"
                            else None)
        quarters = None  # bulk SDWA has no quarters-with-vio column
        formal = prog_raw.get("Feas")
        informal = prog_raw.get("Ifea")
        penalties = None
        inspection_days = None
        compliance_history = None

    return {
        "lead_score": score,
        "score_reasons": " | ".join(reasons),
        "outreach_posture": "no_events",
        "program": program,
        "registry_id": reg_id,
        "company": prog_raw.get("FacName"),
        "address": prog_raw.get("FacStreet"),
        "city": prog_raw.get("FacCity"),
        "state": prog_raw.get("FacState"),
        "zip": prog_raw.get("FacZip"),
        "county": prog_raw.get("FacCounty"),
        "naics": prog_raw.get("FacNAICSCodes"),
        "sic": prog_raw.get("FacSICCodes"),
        "permit_id": prog_raw.get("SourceID"),
        "snc_status": snc_status,
        "violation_status": violation_status,
        "quarters_in_violation": quarters,
        "formal_actions_5yr": formal,
        "informal_actions_5yr": informal,
        "total_penalties_usd": penalties,
        "last_inspection_days_ago": inspection_days,
        "compliance_history_13q": compliance_history,
        "echo_url": ("https://echo.epa.gov/detailed-facility-report"
                     f"?fid={reg_id or ''}"),
        "tag_active_snc": False,
        "tag_treatment_technique": False,
        "tag_mcl_violation": False,
        "tag_lead_copper": False,
        "tag_major_facility": False,
        "tag_only_resolved_events": False,
        "tag_treatable_permit": False,
        "tag_discharges_to_impaired": False,
        "tag_impairment_parameter_match": False,
        "tag_chemtreat_high_relevance": False,
        "__raw": prog_raw,
    }


def _augment_leads(leads: list[dict], events: list[dict],
                   touched_keys: set[tuple] | None = None) -> None:
    """Phase-2 re-score + tag + outreach_posture augmentation.

    Mirrors the equivalent block in `pipeline.run`. Mutates `leads` in
    place. If `touched_keys` is provided, only augments leads whose
    `(registry_id, program)` is in that set — used after the API
    fine-comb so we don't redo work on leads whose events didn't change.
    """
    events_by_key: dict[tuple, list[dict]] = {}
    for ev in events:
        key = (ev.get("registry_id"), ev.get("program"))
        if key[0]:
            events_by_key.setdefault(key, []).append(ev)

    for lead in leads:
        key = (lead["registry_id"], lead["program"])
        if touched_keys is not None and key not in touched_keys:
            continue
        lead_events = events_by_key.get(key, [])
        raw = lead.get("__raw") or {}
        new_score, new_reasons = scoring.score_facility(raw, lead_events)
        lead["lead_score"] = new_score
        lead["score_reasons"] = " | ".join(new_reasons)
        lead["outreach_posture"] = scoring.compute_outreach_posture(lead_events)
        lead.update(scoring.compute_tags(raw, lead_events))


# Floor for the secondary drill-down triggers (newly-discovered,
# score-jumped). Without this, a from-scratch nationwide first-run
# would queue every facility for API drill-down because they're all
# "newly discovered" — easily 100K calls and a guaranteed bot-block.
# 20 captures any lead with at least one substantive scoring rule fire
# (quarters≥1, SNC text without flag, or formal action) without the
# long-tail noise.
SECONDARY_DRILLDOWN_MIN_SCORE = 20


def _drilldown_candidates(leads: list[dict],
                          prior_scores: dict[tuple[str, str], int]
                          ) -> list[dict]:
    """Pick which leads deserve API fine-comb drill-down this run.

    Three independent triggers — any one is sufficient:
      1. `lead_score >= EVENT_DRILLDOWN_MIN_SCORE` — absolute threshold;
         high-value leads always get per-event detail.
      2. Newly-discovered (no prior DB row) AND
         `lead_score >= SECONDARY_DRILLDOWN_MIN_SCORE` — diff signal
         for fresh leads, floored to avoid the from-scratch first-run
         pathological case (drilling every facility we've ever seen).
      3. Score jumped by >10 since the prior run AND
         `lead_score >= SECONDARY_DRILLDOWN_MIN_SCORE` — trajectory
         change worth investigating, same floor for the same reason.

    Leads that already have per-event detail from the bulk feed
    (`outreach_posture != "no_events"`) are excluded — the API drill
    would be redundant.
    """
    out: list[dict] = []
    for lead in leads:
        if lead["outreach_posture"] != "no_events":
            continue
        key = (lead["registry_id"], lead["program"])
        score = lead["lead_score"]
        prior = prior_scores.get(key)
        if score >= EVENT_DRILLDOWN_MIN_SCORE:
            out.append(lead)
        elif score < SECONDARY_DRILLDOWN_MIN_SCORE:
            continue
        elif prior is None:
            out.append(lead)
        elif score > prior + 10:
            out.append(lead)
    return out


def run_bulk(out_dir: Path,
             db_path: Path,
             cache_dir: Path,
             states: list[str] | None = None,
             include_events: bool = True) -> None:
    """End-to-end bulk pipeline. Same output shape as pipeline.run().

    With `include_events=False`, the run makes zero EPA API calls and
    zero event-zip downloads. Useful for air-gapped or rate-limit
    sensitive environments.
    """
    print(LAG_BANNER)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Install warning-collector on the chemtreat logger tree so any
    # WARNING the run emits (bot-block, throttle persistence,
    # high-value-no-events) ends up in run_health.json for the viewer.
    warning_collector = _health.WarningCollector()
    chemtreat_logger = logging.getLogger("chemtreat")
    chemtreat_logger.addHandler(warning_collector)
    drilldown_stats: dict = {}

    try:
        _run_bulk_inner(out_dir, db_path, cache_dir, states, include_events,
                        warning_collector, drilldown_stats)
    finally:
        chemtreat_logger.removeHandler(warning_collector)


def _run_bulk_inner(out_dir: Path, db_path: Path, cache_dir: Path,
                    states: list[str] | None, include_events: bool,
                    warning_collector: "_health.WarningCollector",
                    drilldown_stats: dict) -> None:
    # 1. Snapshot prior scores before any upsert. Used by
    # `_drilldown_candidates` to spot newly-discovered facilities and
    # facilities whose score jumped — both qualify for the fine-comb
    # drill-down even when below the absolute score threshold.
    prior_scores = _load_prior_scores(db_path)
    log.info("Loaded %d prior facility scores for drill-down trigger", len(prior_scores))

    # 2. Download (cached) ECHO Exporter
    exporter_zip = _download_cached(BULK_URLS["echo_exporter"],
                                    cache_dir, "echo_exporter")

    # 3. Stream-filter: yield one row per (facility, program) emission.
    # `_bulk_to_program_shapes` returns a separate raw dict per program
    # — CWA dict has CWA keys only, SDWA dict has SDWA keys only — so
    # the scorer's `or` fallbacks (e.g. CWPFormalEaCnt or Feas) can't
    # mask SDWA values with CWA's "0" string.
    leads: list[dict] = []
    kept_registry_ids: set[str] = set()
    kept_npdes_permits: set[str] = set()
    kept_pwsids: set[str] = set()
    permit_to_registry: dict[str, str] = {}
    pwsid_to_registry: dict[str, str] = {}

    for raw_row, program, prog_raw in stream_echo_exporter(
            exporter_zip, TARGET_NAICS, states):
        # Score per-program (was per-row before — both programs shared
        # one score, which made program-specific facility rules useless).
        score, reasons = scoring.score_facility(prog_raw)
        leads.append(_build_lead_row(prog_raw, program, score, reasons))

        reg = prog_raw.get("RegistryID")
        source = prog_raw.get("SourceID")
        if reg:
            kept_registry_ids.add(reg)
        if program == "CWA" and source:
            # NPDES_IDS in the exporter is sometimes a space-separated list.
            for pid in str(source).split():
                pid = pid.strip()
                if pid:
                    kept_npdes_permits.add(pid)
                    if reg:
                        permit_to_registry[pid] = reg
        elif program == "SDWA" and source:
            for pwsid in str(source).split():
                pwsid = pwsid.strip()
                if pwsid:
                    kept_pwsids.add(pwsid)
                    if reg:
                        pwsid_to_registry[pwsid] = reg

    leads.sort(key=lambda r: r["lead_score"], reverse=True)
    log.info("Kept %d lead rows (%d CWA + %d SDWA) across %d unique facilities",
             len(leads),
             sum(1 for L in leads if L["program"] == "CWA"),
             sum(1 for L in leads if L["program"] == "SDWA"),
             len(kept_registry_ids))

    # Initial tag computation against facility-only signals. This makes
    # `tag_active_snc` etc. correct even in `--no-events` mode where no
    # event-aware phase-2 ever runs. The full phase-2 augmentation below
    # overwrites these once events arrive.
    for lead in leads:
        raw = lead.get("__raw") or {}
        lead.update(scoring.compute_tags(raw))

    # 4. Pre-violation signal augmentation. Gated by `include_events`
    # so the `--no-events` contract ("zero downloads, fully offline";
    # pinned by tests/test_no_events_flag.py) stays intact. Both feeds
    # are NPDES-only — pipeline.run (API path) does not use them today.
    # We load them BEFORE the event feeds so the re-score below picks
    # up the new signals in time for `_drilldown_candidates` selection.
    events: list[dict] = []
    if include_events:
        try:
            limits_zip = _download_cached(BULK_URLS["npdes_limits"],
                                          cache_dir, "npdes_limits")
            permit_signals = stream_permit_limits(limits_zip, kept_npdes_permits)
            permit_hits = 0
            for lead in leads:
                if lead["program"] != "CWA":
                    continue
                sig = permit_signals.get(lead["permit_id"])
                if sig:
                    lead.update(sig)
                    # Mirror onto __raw so score_facility (in
                    # _augment_leads below) sees the columns from the
                    # raw dict — the scorer pulls from `f`, which is
                    # __raw at scoring time.
                    lead["__raw"].update(sig)
                    permit_hits += 1
            log.info("Applied permit-limit signals to %d CWA leads", permit_hits)
        except Exception as e:
            log.warning("Permit-limits bulk load failed: %s", e)

        try:
            attains_zip = _download_cached(BULK_URLS["npdes_attains"],
                                            cache_dir, "npdes_attains")
            impaired_signals = stream_attains_linkage(
                attains_zip, kept_registry_ids, kept_npdes_permits)
            impaired_hits = 0
            for lead in leads:
                # ATTAINS is keyed by RegistryID (the file's primary join
                # key), so both CWA and SDWA leads can pick up signal —
                # though SDWA's facility identity is the PWS, not the
                # outfall, so hit-rate is much lower there.
                sig = impaired_signals.get(lead["registry_id"])
                if sig:
                    lead.update(sig)
                    lead["__raw"].update(sig)
                    impaired_hits += 1
            log.info("Applied ATTAINS impaired-water signals to %d leads",
                     impaired_hits)
        except Exception as e:
            log.warning("ATTAINS bulk load failed: %s", e)

        # Re-score with the new facility-level signals so the new rules
        # (rule_treatable_permit_parameter, rule_discharges_to_impaired)
        # contribute BEFORE drill-down candidate selection. Pass an
        # empty events list — events haven't been loaded yet, and
        # _augment_leads's events=[] path skips EVENT_RULES cleanly.
        _augment_leads(leads, events=[])
        leads.sort(key=lambda r: r["lead_score"], reverse=True)
        log.info("Pre-violation augmentation complete: %d leads now scoring >= %d",
                 sum(1 for L in leads if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE),
                 EVENT_DRILLDOWN_MIN_SCORE)

        log.info("Downloading NPDES events…")
        try:
            npdes_zip = _download_cached(BULK_URLS["npdes"], cache_dir, "npdes")
            events.extend(stream_npdes_violations(
                npdes_zip, kept_registry_ids,
                kept_npdes_permits, permit_to_registry,
            ))
        except Exception as e:
            log.warning("NPDES bulk event load failed: %s", e)

        log.info("Downloading SDWA events…")
        try:
            sdwa_zip = _download_cached(BULK_URLS["sdwa"], cache_dir, "sdwa")
            events.extend(stream_sdwa_violations(
                sdwa_zip, kept_registry_ids,
                kept_pwsids, pwsid_to_registry,
            ))
        except Exception as e:
            log.warning("SDWA bulk event load failed: %s", e)

        # 4a. Phase-2 augmentation: re-score with events, set posture, set tags.
        _augment_leads(leads, events)
        leads.sort(key=lambda r: r["lead_score"], reverse=True)
        log.info("Phase-2 (bulk-events) complete. %d leads scoring >= %d; "
                 "%d with outreach_posture != no_events.",
                 sum(1 for L in leads if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE),
                 EVENT_DRILLDOWN_MIN_SCORE,
                 sum(1 for L in leads if L["outreach_posture"] != "no_events"))

        # 4b. API fine-comb fallback. Triggers: score≥threshold OR
        # newly-discovered (no prior DB row) OR score jumped >10 since
        # prior run. Bulk-only leads that already have events from
        # this run are excluded by `_drilldown_candidates`.
        candidates = _drilldown_candidates(leads, prior_scores)
        drilldown_stats["candidates"] = len(candidates)
        # Leads whose fine-comb drill RAISED (vs. returned cleanly empty),
        # final-attempt outcome. Feeds the Run Health failed-vs-no-data split.
        failed_keys: set = set()
        if candidates:
            log.info("API fine-comb fallback: %d candidates "
                     "(score>=%d / newly-discovered / score-jumped); "
                     "drilling via echo_client...",
                     len(candidates), EVENT_DRILLDOWN_MIN_SCORE)
            end = datetime.utcnow().strftime("%m/%d/%Y")
            start = (datetime.utcnow()
                     - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
            cwa_recovered = _drill_cwa(candidates, start, end, events,
                                       inter_call_sleep=1.0, missed_out=None,
                                       failed_out=failed_keys)
            sdwa_recovered = _drill_sdwa(candidates, events,
                                         inter_call_sleep=2.0, missed_out=None,
                                         failed_out=failed_keys)
            drilldown_stats["cwa_recovered"] = cwa_recovered
            drilldown_stats["sdwa_recovered"] = sdwa_recovered
            log.info("Fine-comb recovered events for %d CWA + %d SDWA leads",
                     cwa_recovered, sdwa_recovered)

            touched_keys = {(L["registry_id"], L["program"]) for L in candidates}
            _augment_leads(leads, events, touched_keys=touched_keys)
            leads.sort(key=lambda r: r["lead_score"], reverse=True)
            still_missing = sum(1 for L in leads
                                if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE
                                and L["outreach_posture"] == "no_events")
            drilldown_stats["still_missing_high_value"] = still_missing
            if still_missing > 0:
                log.warning("After fine-comb: %d high-value leads STILL "
                            "no_events — API retries exhausted. These need "
                            "manual follow-up.", still_missing)

        # Same failed-vs-no-data breakdown the pipeline emits, over the
        # final high-value set. "no_data" here means a lead that had no
        # bulk events AND whose fine-comb came back empty (or had no API
        # identifier) — usually legitimate; "lookup_failed" means the
        # fine-comb drill raised and is worth re-running. Merged alongside
        # the bulk-specific fine-comb stats above (different keys, both
        # consumed by the viewer).
        high_value = [L for L in leads
                      if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE]
        drilldown_stats.update(
            _health.summarize_drilldown(high_value, events, failed_keys, leads))

    # 5. Persist to DB + write standing-state CSVs from DB.
    # Same source-of-truth pattern as pipeline.py.
    run_start_ts = datetime.utcnow().isoformat(timespec="seconds")
    # Per-run folder so this run's CSVs don't overwrite a prior run's.
    run_dir = _run_output_dir(out_dir, "bulk", states, run_start_ts)
    with snapshot.open_db(db_path) as conn:
        fac_diff = snapshot.diff_and_upsert_facilities(conn, leads, now=run_start_ts)
        viol_diff = snapshot.diff_and_upsert_violations(conn, events, now=run_start_ts)
        notes = f"bulk_loader{' --no-events' if not include_events else ''}"
        snapshot.record_run(conn, notes=notes, now=run_start_ts)
        today = datetime.utcnow().strftime("%Y%m%d")
        _write_lag_notice(run_dir)
        snapshot.dump_facilities_csv(conn, run_dir / "all_leads.csv", run_start_ts)
        snapshot.dump_violations_csv(conn, run_dir / "violation_events.csv", run_start_ts)
    _write_csv(run_dir / f"new_facilities_{today}.csv", fac_diff["new"])
    _write_csv(run_dir / f"newly_snc_{today}.csv", fac_diff["newly_snc"])
    _write_csv(run_dir / f"new_violations_{today}.csv", viol_diff["new"])

    health_path = _health.write_run_health(
        run_dir,
        command="bulk_loader",
        states=states,
        include_events=include_events,
        run_start_ts=run_start_ts,
        leads=leads,
        events=events,
        fac_diff=fac_diff,
        viol_diff=viol_diff,
        drilldown_stats=drilldown_stats,
        warnings=warning_collector.records,
        event_drilldown_min_score=EVENT_DRILLDOWN_MIN_SCORE,
        secondary_drilldown_min_score=SECONDARY_DRILLDOWN_MIN_SCORE,
    )
    log.info("Wrote run health to %s", health_path)

    log.info("Bulk run complete: %d leads, %d events, %d new facilities, "
             "%d newly SNC, %d new violations.",
             len(leads), len(events),
             len(fac_diff["new"]), len(fac_diff["newly_snc"]),
             len(viol_diff["new"]))
    log.info("Run outputs in %s — upload all_leads.csv, violation_events.csv, "
             "and run_health.json from there.", run_dir)
    print(LAG_BANNER)


# ----------------------------------------------------- CLI

def _cli() -> None:
    p = argparse.ArgumentParser(
        description="Nationwide ChemTreat lead generator using EPA bulk CSVs.",
    )
    p.add_argument("--out", default="./out", help="Output directory")
    p.add_argument("--db", default="./snapshot.sqlite", help="Snapshot DB path")
    p.add_argument("--cache", default="./cache",
                   help="Where to keep downloaded zips (reused for 7 days)")
    p.add_argument("--states", default=None,
                   help="Optional comma-separated state filter (default: all 50+DC)")
    p.add_argument("--no-events", action="store_true",
                   help="Skip per-event drill-down (faster; facility data only)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    states = [s.strip().upper() for s in args.states.split(",")] if args.states else None
    run_bulk(
        out_dir=Path(args.out),
        db_path=Path(args.db),
        cache_dir=Path(args.cache),
        states=states,
        include_events=not args.no_events,
    )


if __name__ == "__main__":
    _cli()
