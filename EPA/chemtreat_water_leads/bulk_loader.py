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
    TARGET_NAICS, LAG_BANNER, SDWA_LAG_NOTE, CWA_LAG_NOTE, SEWER_LAG_NOTE,
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
    # DMR archive — per-DMR-submission detail (parameter, limit,
    # measured, exceedance %) for the current fiscal year. ~344 MB
    # compressed / ~5 GB unzipped per FY; weekly refresh on current
    # year. Stream-filtered. The cache key embeds the FY so a
    # year-rollover triggers a redownload rather than re-using stale
    # data.
    "dmr_fy2026":    "https://echo.epa.gov/files/echodownloads/npdes_dmrs_fy2026.zip",
    # Sewer Overflow / Bypass events — ~1 MB compressed, DAILY refresh
    # under the NPDES eRule Phase 2 (started 2025-03). Carries the
    # events + types + collection_system_permits CSVs we read; the
    # other six CSVs in the archive are deferred (see CSO_SSO_PLAN.md).
    # Cache window overridden to 1d in run_bulk so the daily cadence
    # actually reaches the lead rows.
    "sewer_overflow": "https://echo.epa.gov/files/echodownloads/current_sewer_overflow_and_collection_systems_tables.zip",
    # National CSO Inventory — ~300 KB compressed, weekly. Supplements
    # the events zip's collection_system_permits.csv with the ~649
    # CSO-system permits whose state hasn't yet onboarded the eRule
    # collection-system reporting.
    "cso_inventory":  "https://echo.epa.gov/files/echodownloads/ALL_CSO_downloads.zip",
}

CACHE_MAX_AGE_DAYS = 7   # ECHO refreshes weekly; cache for that long


# ----------------------------------------------------- download w/ cache

def _download_cached(url: str, cache_dir: Path, name: str,
                     max_age_days: int = CACHE_MAX_AGE_DAYS) -> Path:
    """Download `url` to `cache_dir/name.zip`. Skip if cached file is fresh.

    Caching here is important: the ECHO Exporter is ~250 MB. You don't want
    to pull it on every run. Most EPA feeds refresh weekly so the
    default `CACHE_MAX_AGE_DAYS=7` matches their cadence. The
    sewer-overflow events feed is the exception (daily refresh under
    the 2025 eRule Phase 2) — callers pass `max_age_days=1` for that
    one so the cache doesn't burn 6 of every 7 daily updates.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{name}.zip"

    if target.exists():
        age = datetime.utcnow() - datetime.utcfromtimestamp(target.stat().st_mtime)
        if age < timedelta(days=max_age_days):
            log.info("Using cached %s (%.1f days old, %.1f MB)",
                     name, age.total_seconds() / 86400,
                     target.stat().st_size / 1e6)
            return target
        log.info("Cached %s is %.1f days old (>%d); re-downloading",
                 name, age.total_seconds() / 86400, max_age_days)

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
    # Metals removal is precipitation chemistry — same product family
    # regardless of which metal. IRON and MANGANESE are the highest-
    # volume metals on real permits (~7.7k + ~4.4k rows in a recent
    # sample) and a major scale/discoloration product line; the toxic
    # metals (Cu, Pb, Zn, Ni, Cr, Cd) cover the plating / smelting
    # / industrial wastewater niches. "LEAD," (with the comma) avoids
    # matching "LEADING" — EPA's metals always appear as
    # "Lead, total recoverable" / "Lead, dissolved".
    "metals":           ("COPPER", "LEAD,", "ZINC", "NICKEL",
                         "CHROMIUM", "CADMIUM",
                         "IRON", "MANGANESE"),
    # Cyanide gets its own bucket because cyanide remediation
    # (alkaline chlorination / hydrogen peroxide oxidation) is a
    # distinct product line from metals precipitation. Plating shops
    # and electronics manufacturers are the canonical buyers. Real
    # permits use "Cyanide, total [as CN]" / "Cyanide, free
    # available" / "Cyanide, weak acid, dissociable" — all caught by
    # the "CYANIDE" substring.
    "cyanide":          ("CYANIDE",),
    "chlorine_residual": ("CHLORINE, TOTAL RESIDUAL",
                          "TOTAL RESIDUAL CHLORINE"),
    # Microbiological control. ChemTreat explicitly includes microbial
    # treatment (biocides, disinfection chemistry) as a product line,
    # so coliform / E. coli / Enterococci / fecal-indicator exceedances
    # ARE a sales angle — they belong in the treatable bucket alongside
    # BOD and metals, not in "worst-exceedance-but-can't-help" territory.
    # EPA's wording is highly variable: "Coliform, fecal general",
    # "Fecal coliform", "E. coli" / "Escherichia coli", "Enterococci"
    # / "Enterococcus". "E. COLI" needs the period because EPA never
    # writes "ECOLI". "ENTEROCOCC" catches both singular and plural.
    # "FECAL" alone is safe — any EPA pollutant prefixed "fecal" is a
    # microbial indicator.
    "microbiological":  ("COLIFORM", "E. COLI", "ESCHERICHIA",
                          "ENTEROCOCC", "FECAL", "PATHOGEN"),
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


# ----------------------------------------------------- DMR exceedances
#
# npdes_dmrs_fyYYYY.zip contains one row per (permit × outfall × parameter
# × monitoring period × statistical basis) DMR submission across the fiscal
# year. The exceedance column is server-side computed by EPA so we don't
# have to do limit-vs-measured math — but it's spelled `EXCEEDENCE_PCT`
# in the live file (EPA typo, not in their docs as `EXCEEDANCE_PCT`).
# Verified against npdes_dmr_fy2026.csv 2026-05-30 refresh; do not
# "correct" the spelling in the code without re-verifying first.
#
# File is ~5 GB unzipped per FY; stream-filter is mandatory.

# How many distinct exceeded parameter descriptions to capture in
# top_exceeded_parameter and exceeded_treatable_parameters_text per
# permit. A permit with 20 different parameters all exceeding is a
# crisis, but the viewer renders these as a single cell — caps at the
# same ~15 as permitted_parameters_text.
_MAX_EXCEEDED_PARAMS_SAMPLED = 15


def _safe_pct(raw: str) -> float | None:
    """Parse EXCEEDENCE_PCT into a float, or None for blank/unparseable.

    EPA's column is sparsely populated — most rows are compliant and
    leave it blank. A literal "0" also appears. We want None for both
    so the rollup ignores them (only > 0 counts as an exceedance)."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


# When a permit's LIMIT_VALUE is 0, EPA reports EXCEEDENCE_PCT as
# INT32_MAX (2,147,483,647 — verified in the live FY2026 file on
# permits like AKG528836 "Seafood Processing Waste"). The value
# encodes "infinite exceedance over a zero limit", not a real
# percentage. The +15 tier still applies (it's correctly severe), but
# the displayed raw number is meaningless. Clamp at 99,999% so the
# CSV and viewer render a readable upper bound — anything above the
# +15 tier threshold (1000%) is sorted equivalently anyway.
_DISPLAY_EXCEEDANCE_CAP = 99_999.0


def _clamp_pct_for_display(pct: float) -> float:
    return min(pct, _DISPLAY_EXCEEDANCE_CAP)


def stream_dmr_exceedances(
    zip_path: Path,
    kept_npdes_permits: set[str],
    permit_to_registry: dict[str, str] | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Build per-NPDES-permit exceedance signals AND per-row event
    rows from a DMR archive zip.

    Returns ``(signals_by_permit, events)`` where:

      signals_by_permit = {
        npdes_id: {
          "recent_dmr_exceedances_count": int,
          "top_exceeded_parameter": str,
          "top_exceedance_pct": float,
          "exceeded_treatable_parameters_text": str,  # "bod | phosphorus"
        }
      }

      events = [
        {"violation_id", "registry_id", "program",
         "parameter", "limit_value", "limit_unit",
         "dmr_value", "dmr_unit", "exceedance_pct",
         "period_end", "violation_code", "npdes_id",
         "status", "stat_basis", ...},
        ...
      ]

    The signals dict feeds rule_recent_dmr_exceedance and
    rule_exceeds_treatable_parameter. The events list feeds
    snapshot.diff_and_upsert_violations — these are exactly the
    per-DMR rows the existing NPDES_SE/PS/CS streamer can't supply
    parameter detail for (bulk NPDES violation files don't carry
    LIMIT/DMR value columns). NPDES_VIOLATION_ID dedupes via the
    violations table's PK, so where both feeds emit the same
    violation_id, the DMR-archive emission overwrites the
    NPDES_SE emission's empty parameter fields with the populated
    ones.

    Filter:
      * `EXTERNAL_PERMIT_NMBR in kept_npdes_permits` (same filter as
        stream_permit_limits — identical join key).
      * `EXCEEDENCE_PCT > 0` (blank / 0 / unparseable rows skipped).
    """
    if not kept_npdes_permits:
        log.info("No CWA permits in scope; skipping DMR exceedance scan.")
        return {}, []

    permit_to_registry = permit_to_registry or {}
    out: dict[str, dict] = {}
    events: list[dict] = []
    # Per-permit accumulators: distinct treatable classes seen
    # exceeded, and distinct parameter descriptions for the text
    # field. Tracked separately from `out` so the rollup at the end
    # can render them.
    treatable_by_permit: dict[str, set[str]] = {}
    params_by_permit: dict[str, set[str]] = {}
    rows_scanned = rows_kept = 0

    with zipfile.ZipFile(zip_path) as zf:
        # File pattern is NPDES_DMRS_FY<YEAR>.csv — match the suffix
        # so we work across fiscal years without hard-coding.
        target = next(
            (n for n in zf.namelist()
             if n.upper().startswith("NPDES_DMRS_FY")
             and n.lower().endswith(".csv")),
            None,
        )
        if target is None:
            raise RuntimeError(
                f"No NPDES_DMRS_FY*.csv inside {zip_path.name}; EPA may "
                "have renamed the file — check "
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
                pct = _safe_pct(row.get("EXCEEDENCE_PCT"))
                if pct is None:
                    continue
                param_desc = (row.get("PARAMETER_DESC") or "").strip()
                if not param_desc:
                    continue
                rows_kept += 1

                # ----- Per-permit rollup -----
                # Clamp top_exceedance_pct for storage/display only;
                # the event payload keeps the raw float so violation
                # rows preserve the EPA sentinel for downstream audit.
                clamped = _clamp_pct_for_display(pct)
                sig = out.setdefault(permit, {
                    "recent_dmr_exceedances_count": 0,
                    "top_exceeded_parameter": param_desc,
                    "top_exceedance_pct": clamped,
                })
                sig["recent_dmr_exceedances_count"] += 1
                if clamped > sig["top_exceedance_pct"]:
                    sig["top_exceedance_pct"] = clamped
                    sig["top_exceeded_parameter"] = param_desc

                cls = _classify_parameter(param_desc)
                if cls is not None:
                    treatable_by_permit.setdefault(permit, set()).add(cls)

                pset = params_by_permit.setdefault(permit, set())
                if len(pset) < _MAX_EXCEEDED_PARAMS_SAMPLED:
                    pset.add(param_desc)

                # ----- Per-row event payload -----
                # registry_id backfilled from permit_to_registry (the
                # same lookup table that backfills stream_npdes_violations
                # events). Without this, the snapshot's
                # events_by_key={(registry_id, program)} join in
                # _augment_leads would silently drop these events.
                events.append({
                    "violation_id": row.get("NPDES_VIOLATION_ID"),
                    "registry_id": permit_to_registry.get(permit),
                    "permit_id": permit,
                    "npdes_id": permit,
                    "program": "CWA",
                    "parameter": param_desc,
                    "limit_value": row.get("LIMIT_VALUE_STANDARD_UNITS")
                                   or row.get("LIMIT_VALUE_NMBR"),
                    "limit_unit": row.get("STANDARD_UNIT_DESC")
                                  or row.get("LIMIT_UNIT_DESC"),
                    "dmr_value": row.get("DMR_VALUE_STANDARD_UNITS")
                                 or row.get("DMR_VALUE_NMBR"),
                    "dmr_unit": row.get("DMR_UNIT_DESC")
                                or row.get("STANDARD_UNIT_DESC"),
                    "exceedance_pct": pct,
                    "period_end": row.get("MONITORING_PERIOD_END_DATE"),
                    "violation_code": row.get("VIOLATION_CODE"),
                    "stat_basis": row.get("STATISTICAL_BASE_TYPE_CODE"),
                    "status": "Unresolved",
                    "data_lag_note": CWA_LAG_NOTE,
                })

                if rows_scanned % 1_000_000 == 0:
                    log.info("  DMR scanned %d rows, kept %d exceedances",
                             rows_scanned, rows_kept)

    # Finalize text fields.
    for permit, classes in treatable_by_permit.items():
        out[permit]["exceeded_treatable_parameters_text"] = " | ".join(
            sorted(classes))
    for permit, params in params_by_permit.items():
        # No public column for this today; available via top_exceeded
        # and the per-event detail. Stored anyway for future use; the
        # snapshot upsert ignores keys it doesn't know.
        out[permit]["exceeded_parameters_text"] = " | ".join(sorted(params))

    log.info("DMR exceedances done: scanned %d rows, kept %d exceedance "
             "rows across %d permits, emitted %d events",
             rows_scanned, rows_kept, len(out), len(events))
    return out, events


# ----------------------------------------------------- sewer overflow events
#
# current_sewer_overflow_and_collection_systems_tables.zip contains nine
# CSVs (eight data + one column-metadata catalog) plus an ERD PDF. We
# read three of them: the events backbone, the one-to-many types lookup,
# and the collection-system permit enrollment (which carries CSS percent
# + population — feeds rule_collection_system_population alongside the
# event-based scoring).
#
# Schema pinned against the 2026-06-15 refresh (915 KB compressed, 4,221
# events, 608 distinct permits, 15 states/territories reporting). EPA's
# column names are LOWERCASE_SNAKE — verify with the header-dump in
# CSO_SSO_PLAN.md if anything looks off.
#
# Reporting only began 2025-03 under the NPDES eRule Phase 2 and rolls
# on state-by-state, so empty results are expected for many states. The
# streamer surfaces a `SEWER_LAG_NOTE` on every event row.

_SEWER_EVENTS_CSV = "sewer_overflow_bypass_report_events.csv"
_SEWER_TYPES_CSV = "sewer_overflow_bypass_types.csv"
_SEWER_PERMITS_CSV = "collection_system_permits.csv"

# Per-event datetime format. EPA writes naive timestamps like
# "2025-09-09 20:30:00" — no timezone, treat as UTC-equivalent for the
# window comparison (we're not doing anything timezone-sensitive).
_SEWER_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_sewer_datetime(s: str) -> datetime | None:
    """Parse EPA's sewer-event datetime. Returns None on blank /
    unparseable so the window filter ignores those rows safely."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, _SEWER_DATETIME_FMT)
    except ValueError:
        return None


def _safe_volume_gallons(raw: str) -> float | None:
    """Parse a volume-gallons cell into a float, or None for blank /
    unparseable / zero.

    EPA reports ~30% of events with no volume — leaves the cell blank
    rather than zeroing it. Treat None and 0 the same (don't add to
    the sum, don't influence the tier)."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def stream_sewer_overflow_events(
    zip_path: Path,
    kept_npdes_permits: set[str],
    permit_to_registry: dict[str, str] | None = None,
    window_days: int = 365,
    now: datetime | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Build per-permit sewer-overflow signals AND per-event rows from
    `current_sewer_overflow_and_collection_systems_tables.zip`.

    Returns ``(signals_by_permit, events)`` where:

      signals_by_permit = {
        npdes_id: {
          "recent_sewer_overflow_count": int,
          "recent_sewer_overflow_volume_gal": float,
          "most_recent_sewer_overflow_at": str,    # ISO datetime
          "recent_sewer_overflow_types": str,      # "CSO | SSO"
          "has_dry_weather_overflow": int,         # 0/1
        }
      }

      events = [
        {"violation_id", "registry_id", "permit_id", "npdes_id",
         "program", "violation_category", "parameter",
         "dmr_value", "dmr_unit", "period_begin", "period_end",
         "violation_description", "status", "data_lag_note", ...},
        ...
      ]

    Filter:
      * `permit_identifier in kept_npdes_permits`. Same shape as
        stream_dmr_exceedances — identical join key.
      * Event start within `window_days` of `now` (default
        `datetime.utcnow()`).

    Streaming notes:
      * Pre-loads the types CSV into `{event_key: sorted [type_codes]}`
        before scanning events. The types table is one-to-many (a
        single event can carry multiple type codes — e.g. SSO and BYP
        for an overflow that occurred during a bypass).
      * `sewer_overflow_bypass_event_key` is EPA's stable system-
        generated PK; used verbatim as `violation_id`.
      * Datetimes are EPA's `"YYYY-MM-DD HH:MM:SS"` (naive). Rows with
        unparseable / blank start datetime are kept but excluded from
        the window cutoff (defensive — we'd rather see them than drop).
    """
    if not kept_npdes_permits:
        log.info("No CWA permits in scope; skipping sewer-overflow scan.")
        return {}, []

    permit_to_registry = permit_to_registry or {}
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=window_days)

    with zipfile.ZipFile(zip_path) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        events_name = names.get(_SEWER_EVENTS_CSV)
        types_name = names.get(_SEWER_TYPES_CSV)
        if events_name is None or types_name is None:
            raise RuntimeError(
                f"Missing {_SEWER_EVENTS_CSV} or {_SEWER_TYPES_CSV} inside "
                f"{zip_path.name}; EPA may have renamed a file — check "
                "https://echo.epa.gov/tools/data-downloads"
            )

        # ----- Pre-load event-key → [type_codes] from the types CSV -----
        # The types table is small (~4K rows on a typical refresh, one
        # row per (event, type)). Loading it whole is cheap; the join
        # against the events scan stays in-memory and O(events).
        types_by_event: dict[str, set[str]] = {}
        with zf.open(types_name) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                ek = row.get("sewer_overflow_bypass_event_key")
                code = (row.get("sewer_overflow_bypass_type_code") or "").strip().upper()
                if ek and code:
                    types_by_event.setdefault(ek, set()).add(code)
        log.info("Sewer types: loaded %d event→types entries", len(types_by_event))

        # ----- Scan events ---------------------------------------------
        out: dict[str, dict] = {}
        events: list[dict] = []
        types_by_permit: dict[str, set[str]] = {}
        rows_scanned = rows_kept = 0

        with zf.open(events_name) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                rows_scanned += 1
                permit = row.get("permit_identifier")
                if not permit or permit not in kept_npdes_permits:
                    continue
                event_key = row.get("sewer_overflow_bypass_event_key")
                if not event_key:
                    # No PK — can't dedupe across refreshes. Skip rather
                    # than synthesize; the upstream-data contract says
                    # this should always be populated.
                    continue

                start_raw = row.get("sewer_overflow_bypass_start_datetime") or ""
                start_dt = _parse_sewer_datetime(start_raw)
                # Window filter: skip events with a parseable start older
                # than the cutoff. Unparseable / blank starts are KEPT —
                # defensive, since the file's "current" snapshot already
                # implies recency.
                if start_dt is not None and start_dt < cutoff:
                    continue

                rows_kept += 1
                end_raw = row.get("sewer_overflow_bypass_end_datetime") or ""
                volume = _safe_volume_gallons(
                    row.get("sewer_overflow_bypass_discharge_volume_gallons"))
                wet = (row.get("wet_weather_occurance_indicator") or "").strip().upper()
                structure = (row.get("sewer_overflow_structure_type_desc") or "").strip()
                type_codes = types_by_event.get(event_key, set())

                # ----- Per-permit rollup --------------------------------
                sig = out.setdefault(permit, {
                    "recent_sewer_overflow_count": 0,
                    "recent_sewer_overflow_volume_gal": 0.0,
                    "most_recent_sewer_overflow_at": "",
                    "has_dry_weather_overflow": 0,
                })
                sig["recent_sewer_overflow_count"] += 1
                if volume is not None:
                    sig["recent_sewer_overflow_volume_gal"] += volume
                if wet == "N":
                    sig["has_dry_weather_overflow"] = 1
                # most_recent_sewer_overflow_at: track the newest
                # parseable start datetime. Falls back to start_raw
                # only when we have nothing better — preserves the
                # field for unparseable rows so they still surface.
                if start_dt is not None:
                    prev = sig["most_recent_sewer_overflow_at"]
                    if not prev or start_raw > prev:
                        sig["most_recent_sewer_overflow_at"] = start_raw
                elif not sig["most_recent_sewer_overflow_at"]:
                    sig["most_recent_sewer_overflow_at"] = start_raw

                if type_codes:
                    types_by_permit.setdefault(permit, set()).update(type_codes)

                # ----- Per-event payload --------------------------------
                # Map type code(s) to a single human-readable parameter
                # for the violation_events.csv. Order of preference SSO
                # > CSO > BYP matches the tier ordering — when an event
                # carries multiple codes, the row reads as the most
                # diagnostic.
                if "SSO" in type_codes:
                    parameter = "Sanitary Sewer Overflow"
                elif "CSO" in type_codes:
                    parameter = "Combined Sewer Overflow"
                elif "BYP" in type_codes:
                    parameter = "Bypass"
                else:
                    parameter = "Sewer Overflow"

                # violation_description: structure + wet/dry tag so the
                # viewer's event-detail row reads cleanly without the
                # rep having to know what wet_weather_occurance_indicator
                # means.
                desc_parts = []
                if structure:
                    desc_parts.append(structure)
                if wet == "N":
                    desc_parts.append("dry-weather")
                elif wet == "Y":
                    desc_parts.append("wet-weather")
                description = " — ".join(desc_parts) if desc_parts else ""

                events.append({
                    "violation_id": event_key,
                    "registry_id": permit_to_registry.get(permit),
                    "permit_id": permit,
                    "npdes_id": permit,
                    "program": "CWA",
                    "violation_category": "Sewer Overflow / Bypass Event",
                    "parameter": parameter,
                    "dmr_value": (f"{volume:.0f}" if volume is not None else ""),
                    "dmr_unit": "gallons" if volume is not None else "",
                    "period_begin": start_raw,
                    "period_end": end_raw,
                    "violation_description": description,
                    "status": "Unresolved",
                    "data_lag_note": SEWER_LAG_NOTE,
                })

                if rows_scanned % 100_000 == 0:
                    log.info("  Sewer overflow scanned %d rows, kept %d",
                             rows_scanned, rows_kept)

    # Finalize per-permit types text. Sorted for stable snapshot diffs
    # (same convention as stream_attains_linkage's joins).
    for permit, codes in types_by_permit.items():
        out[permit]["recent_sewer_overflow_types"] = " | ".join(sorted(codes))

    log.info("Sewer overflow events done: scanned %d rows, kept %d across "
             "%d permits", rows_scanned, rows_kept, len(out))
    return out, events


def stream_collection_system_permits(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Build per-permit collection-system signals from
    `collection_system_permits.csv` inside the sewer-overflow events
    zip. Static enrollment data — one row per (permit, collection-system
    identifier).

    Returns ``{npdes_id: {percent_collection_system_css: int,
                          collection_system_population: int,
                          has_combined_sewer_system: 0/1}}``.

    Aggregation across multi-system permits (rare — ~30 of the 4,036
    permits in the 2026-06-15 refresh have >1 system):
      * `collection_system_population`: SUM across sub-systems. A POTW
        whose collection network feeds 6 municipalities at 5K each is
        a 30K-population account, not 5K — the rep cares about total
        served.
      * `percent_collection_system_css`: MAX across sub-systems. The
        question the score asks is "does this permit operate ANY
        combined sewer system?" — present in even one feeds the same
        wet-weather-overflow risk that drives the +5 rule.
      * `has_combined_sewer_system`: 1 if any sub-system has css_pct > 0.

    Filter:
      * `permit_identifier in kept_npdes_permits`. Same join key as
        the events feed.
    """
    if not kept_npdes_permits:
        log.info("No CWA permits in scope; skipping collection-system scan.")
        return {}

    out: dict[str, dict] = {}
    rows_scanned = rows_matched = 0

    with zipfile.ZipFile(zip_path) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        target = names.get(_SEWER_PERMITS_CSV)
        if target is None:
            raise RuntimeError(
                f"No {_SEWER_PERMITS_CSV} inside {zip_path.name}; EPA may "
                "have renamed the file — check "
                "https://echo.epa.gov/tools/data-downloads"
            )
        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                rows_scanned += 1
                permit = row.get("permit_identifier")
                if not permit or permit not in kept_npdes_permits:
                    continue
                rows_matched += 1
                try:
                    pop = int(float(row.get("collection_system_population") or 0))
                except ValueError:
                    pop = 0
                try:
                    css_pct = int(float(row.get("percent_collection_system_css") or 0))
                except ValueError:
                    css_pct = 0

                sig = out.setdefault(permit, {
                    "percent_collection_system_css": 0,
                    "collection_system_population": 0,
                    "has_combined_sewer_system": 0,
                })
                # SUM population (multi-system permits cover larger
                # service areas; the score rule reads total served).
                sig["collection_system_population"] += pop
                # MAX css_pct (any one combined system flips the bit).
                if css_pct > sig["percent_collection_system_css"]:
                    sig["percent_collection_system_css"] = css_pct
                if css_pct > 0:
                    sig["has_combined_sewer_system"] = 1

    log.info("Collection-system permits done: scanned %d rows, matched %d, "
             "kept signal for %d permits",
             rows_scanned, rows_matched, len(out))
    return out


def stream_cso_inventory(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Build per-permit CSS signals from the National CSO Inventory
    (`ALL_CSO_DOWNLOADS.csv`). Supplements
    `stream_collection_system_permits` — covers ~649 CSO-system permits
    in the 2026-06-15 refresh that the eRule-driven collection-system
    data hasn't onboarded yet (older permits whose state hasn't started
    reporting).

    Returns ``{npdes_id: {has_combined_sewer_system: 1}}``.

    File shape: one row per CSO outfall, many rows per permit. We only
    need the existence of any matching row — every row in this file
    documents a CSO outfall by definition, so presence == CSS POTW.

    Filter:
      * `NPDES_ID in kept_npdes_permits`. Note the column is uppercase
        here (`NPDES_ID`) vs the eRule files' `permit_identifier` —
        EPA's two feeds use different conventions. Both join to the
        same NPDES permit-id space.
    """
    if not kept_npdes_permits:
        log.info("No CWA permits in scope; skipping CSO inventory scan.")
        return {}

    out: dict[str, dict] = {}
    rows_scanned = rows_matched = 0

    with zipfile.ZipFile(zip_path) as zf:
        target = next(
            (n for n in zf.namelist()
             if n.upper().endswith("ALL_CSO_DOWNLOADS.CSV")),
            None,
        )
        if target is None:
            raise RuntimeError(
                f"No ALL_CSO_DOWNLOADS.csv inside {zip_path.name}; EPA may "
                "have renamed the file — check "
                "https://echo.epa.gov/tools/data-downloads"
            )
        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8",
                                       errors="replace")
            for row in csv.DictReader(text_fh):
                rows_scanned += 1
                permit = row.get("NPDES_ID")
                if not permit or permit not in kept_npdes_permits:
                    continue
                rows_matched += 1
                # Many rows per permit — set once, ignore subsequent
                # matches. The signal is binary; we'd just be re-writing
                # the same 1.
                out.setdefault(permit, {"has_combined_sewer_system": 1})

    log.info("CSO inventory done: scanned %d rows, matched %d outfalls, "
             "kept signal for %d permits",
             rows_scanned, rows_matched, len(out))
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
        # SDWA-only PWS metadata. ECHO Exporter doesn't carry these at
        # the facility level (verified empirically — see MEMORY.md
        # "SDWA bulk has limited facility-level signals"), so bulk leads
        # leave them None. Schema slot stays populated so snapshot's
        # column shape matches across paths. API fine-comb DFR drills
        # have the data in their response but don't currently backfill;
        # if sales asks for bulk-side population coverage that's a
        # focused follow-up on _drill_sdwa.
        "population_served": None,
        "system_type": None,
        "owner_type": None,
        "primary_source": None,
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
        "tag_recent_exceedance": False,
        "tag_exceeds_treatable_parameter": False,
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


# SNC text patterns that mark a lead as un-actionable for API fine-comb.
# A high lead_score driven by one of these reflects a regulatory state
# EPA's per-event endpoints won't surface anything useful for:
#
#   - "Failure to Report" / "DMR - Not Received": Reporting Non-Compliance.
#     The facility missed paperwork; no chemistry-relevant exceedance to
#     drill. EPA's DMR archive shows NODI='C' (no discharge) rows for
#     these — verified on KSG110024 in the 2026-06-12 run. Drilling
#     burns API budget for zero recovery and contributes to the 20-streak
#     429 short-circuit that breaks fine-comb for actionable leads.
#   - "Terminated Permit": permit closed; nothing current to surface.
#   - "No Violation Identified": SNC text explicitly says no current
#     violation. Score carried by historical formal_actions_5yr; per-event
#     drill returns nothing.
#
# Empirical basis: 2026-06-12 nationwide run queued 993 high-value leads
# for fine-comb; 724 (73%) hit one of these patterns and would not have
# surfaced events even on a clean EPA response.
_UNACTIONABLE_SNC_PATTERNS = (
    "failure to report",
    "dmr - not received",
    "terminated permit",
    "no violation identified",
)


def _is_unactionable_for_drilldown(lead: dict) -> bool:
    """True if SNC text marks the lead as un-actionable for API fine-comb.

    Lead stays in the inventory — score reflects real signals (past
    enforcement, missed reporting) — but per-event API drill would
    return nothing AND counts against EPA's per-IP throttle bucket.
    """
    txt = (lead.get("snc_status") or "").lower()
    if not txt:
        return False
    return any(p in txt for p in _UNACTIONABLE_SNC_PATTERNS)


def _drilldown_candidates(leads: list[dict],
                          prior_scores: dict[tuple[str, str], int],
                          prior_eligibility: dict[tuple[str, str], str] | None = None,
                          now_iso: str | None = None,
                          ) -> list[dict]:
    """Pick which leads deserve API fine-comb drill-down this run.

    Three independent positive triggers — any one is sufficient:
      1. `lead_score >= EVENT_DRILLDOWN_MIN_SCORE` — absolute threshold;
         high-value leads always get per-event detail.
      2. Newly-discovered (no prior DB row) AND
         `lead_score >= SECONDARY_DRILLDOWN_MIN_SCORE` — diff signal
         for fresh leads, floored to avoid the from-scratch first-run
         pathological case (drilling every facility we've ever seen).
      3. Score jumped by >10 since the prior run AND
         `lead_score >= SECONDARY_DRILLDOWN_MIN_SCORE` — trajectory
         change worth investigating, same floor for the same reason.

    Exclusions:
      * Leads that already have per-event detail from the bulk feed
        (`outreach_posture != "no_events"`) — the API drill would be
        redundant.
      * Leads whose SNC text marks them un-actionable for fine-comb
        (`_is_unactionable_for_drilldown` — RNC-only, terminated
        permit, or "no violation identified"). EPA's per-event
        endpoints have nothing chemistry-relevant to return for these;
        skipping them preserves the fine-comb budget for genuine
        current violations and avoids tripping the 429 short-circuit.
      * Leads still in their drill-down backoff window — i.e. whose
        `next_drilldown_eligible_at` is in the future. When
        `prior_eligibility` is supplied, this filter applies AFTER
        the positive triggers and the bulk-events exclusion: a lead
        that would otherwise be a candidate gets skipped because the
        per-outcome backoff (`pipeline.DRILLDOWN_BACKOFF`) says we
        drilled it recently and a retry would just re-trip the same
        throttle. Without `prior_eligibility`, behavior matches the
        pre-2026-06-09 design (no backoff filter).

    `now_iso` lets callers pin the comparison clock — ISO strings
    compare lexically when same-format, so this is exact. Defaults to
    `datetime.utcnow().isoformat(timespec="seconds")`.
    """
    if prior_eligibility is None:
        prior_eligibility = {}
    if now_iso is None:
        now_iso = datetime.utcnow().isoformat(timespec="seconds")

    out: list[dict] = []
    for lead in leads:
        if lead["outreach_posture"] != "no_events":
            continue
        if _is_unactionable_for_drilldown(lead):
            continue
        key = (lead["registry_id"], lead["program"])
        score = lead["lead_score"]
        prior = prior_scores.get(key)
        # Positive trigger check first — same three rules as before.
        is_candidate = False
        if score >= EVENT_DRILLDOWN_MIN_SCORE:
            is_candidate = True
        elif score < SECONDARY_DRILLDOWN_MIN_SCORE:
            continue
        elif prior is None:
            is_candidate = True
        elif score > prior + 10:
            is_candidate = True
        if not is_candidate:
            continue
        # Backoff gate. The lead would otherwise be a candidate, but
        # if we drilled it recently and the per-outcome window hasn't
        # elapsed, skip — re-drilling is just going to re-trip EPA's
        # throttle (for 'lookup_failed') or re-confirm an unchanged
        # 'no_data' answer.
        eligible_at = prior_eligibility.get(key)
        if eligible_at and eligible_at > now_iso:
            continue
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
    # 1. Snapshot prior scores + drill-down streaks before any upsert.
    # `prior_scores` drives `_drilldown_candidates` (newly-discovered /
    # score-jumped triggers); `prior_streaks` drives the streak math in
    # `_record_drilldown_outcome` so a fresh 'lookup_failed' increments
    # the right base.
    prior_scores = _load_prior_scores(db_path)
    log.info("Loaded %d prior facility scores for drill-down trigger", len(prior_scores))
    with snapshot.open_db(db_path) as conn:
        prior_streaks = snapshot.load_prior_drilldown_state(conn)
        prior_eligibility = snapshot.load_prior_drilldown_eligibility(conn)
    log.info("Loaded %d prior drill-down failure streaks; "
             "%d rows in backoff (eligibility-at populated)",
             len(prior_streaks), len(prior_eligibility))

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

        # DMR archive: per-permit exceedance signals + per-DMR event
        # rows. Signals applied to leads here so the new rules
        # (rule_recent_dmr_exceedance, rule_exceeds_treatable_parameter)
        # contribute to pre-rescoring drill-down selection. Events held
        # for later — they get appended AFTER the bulk NPDES/SDWA event
        # feeds so that snapshot's per-violation_id upsert lets the
        # DMR-archive emission overwrite the NPDES_SE emission's empty
        # parameter fields. See stream_dmr_exceedances docstring for
        # the dedup contract.
        dmr_events: list[dict] = []
        try:
            dmr_zip = _download_cached(BULK_URLS["dmr_fy2026"],
                                       cache_dir, "dmr_fy2026")
            dmr_signals, dmr_events = stream_dmr_exceedances(
                dmr_zip, kept_npdes_permits, permit_to_registry)
            dmr_hits = 0
            for lead in leads:
                if lead["program"] != "CWA":
                    continue
                sig = dmr_signals.get(lead["permit_id"])
                if sig:
                    lead.update(sig)
                    lead["__raw"].update(sig)
                    dmr_hits += 1
            log.info("Applied DMR exceedance signals to %d CWA leads "
                     "(%d total event rows queued for persistence)",
                     dmr_hits, len(dmr_events))
        except Exception as e:
            log.warning("DMR archive load failed: %s", e)

        # Sewer Overflow / Bypass — daily-cadence active-compliance
        # signal. Cache window pinned to 1 day so the daily refresh
        # actually reaches the lead rows (default would coalesce 7 of
        # them). Signals applied immediately so the new rules
        # (rule_recent_sewer_overflow, rule_combined_sewer_system,
        # rule_collection_system_population) contribute to drill-down
        # candidate selection. Events held alongside dmr_events for
        # later append — same dedup convention as DMR-vs-NPDES_SE.
        sewer_events: list[dict] = []
        try:
            sewer_zip = _download_cached(BULK_URLS["sewer_overflow"],
                                         cache_dir, "sewer_overflow",
                                         max_age_days=1)
            sewer_signals, sewer_events = stream_sewer_overflow_events(
                sewer_zip, kept_npdes_permits, permit_to_registry)
            sewer_hits = 0
            for lead in leads:
                if lead["program"] != "CWA":
                    continue
                sig = sewer_signals.get(lead["permit_id"])
                if sig:
                    lead.update(sig)
                    lead["__raw"].update(sig)
                    sewer_hits += 1
            log.info("Applied sewer-overflow signals to %d CWA leads "
                     "(%d total event rows queued for persistence)",
                     sewer_hits, len(sewer_events))
        except Exception as e:
            log.warning("Sewer overflow load failed: %s", e)

        # Collection-system permits — static enrollment data, lives in
        # the sewer_overflow zip we already downloaded above. Cheap to
        # scan even on its own. Feeds rule_combined_sewer_system (+5)
        # and rule_collection_system_population (POTW revenue proxy).
        try:
            # Re-resolve the zip path; if the earlier download failed,
            # this block will fail too and the warning chain will land
            # twice in the log — acceptable (the second message
            # explains exactly which feed died).
            sewer_zip = _download_cached(BULK_URLS["sewer_overflow"],
                                         cache_dir, "sewer_overflow",
                                         max_age_days=1)
            cs_signals = stream_collection_system_permits(
                sewer_zip, kept_npdes_permits)
            cs_hits = 0
            for lead in leads:
                if lead["program"] != "CWA":
                    continue
                sig = cs_signals.get(lead["permit_id"])
                if sig:
                    lead.update(sig)
                    lead["__raw"].update(sig)
                    cs_hits += 1
            log.info("Applied collection-system permit signals to %d CWA leads",
                     cs_hits)
        except Exception as e:
            log.warning("Collection-system permits load failed: %s", e)

        # National CSO Inventory — covers the ~649 CSO-system permits
        # the eRule data hasn't onboarded yet. ONLY emits
        # has_combined_sewer_system=1; sets the flag on permits where
        # collection_system_permits.csv didn't (either absent or
        # css_pct=0). The OR semantics are intentional: a permit
        # listed in the federal CSO inventory has CSO outfalls by
        # definition.
        try:
            cso_zip = _download_cached(BULK_URLS["cso_inventory"],
                                       cache_dir, "cso_inventory")
            cso_signals = stream_cso_inventory(
                cso_zip, kept_npdes_permits)
            cso_hits = 0
            for lead in leads:
                if lead["program"] != "CWA":
                    continue
                sig = cso_signals.get(lead["permit_id"])
                if sig:
                    lead.update(sig)
                    lead["__raw"].update(sig)
                    cso_hits += 1
            log.info("Applied CSO inventory signals to %d CWA leads", cso_hits)
        except Exception as e:
            log.warning("CSO inventory load failed: %s", e)

        # Re-score with the new facility-level signals so the new rules
        # (rule_treatable_permit_parameter, rule_discharges_to_impaired,
        # rule_recent_dmr_exceedance, rule_exceeds_treatable_parameter,
        # rule_recent_sewer_overflow, rule_combined_sewer_system,
        # rule_collection_system_population) contribute BEFORE drill-down
        # candidate selection. Pass an empty events list — events
        # haven't been loaded yet, and _augment_leads's events=[] path
        # skips EVENT_RULES cleanly.
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

        # DMR-archive events go LAST so snapshot's per-violation_id
        # upsert lets them overwrite the NPDES_SE emission's empty
        # parameter fields. The two feeds share NPDES_VIOLATION_ID for
        # ~82% of exceedance rows (verified in the live FY2026 file);
        # the rest are DMR-only. Either way, DMR-last wins on conflict.
        events.extend(dmr_events)
        # Sewer-overflow events ride a disjoint violation_id space
        # (EPA's sewer_overflow_bypass_event_key is unique to this
        # feed), so order doesn't affect upsert behavior — appended
        # alongside DMR for symmetry. snapshot.diff_and_upsert_
        # violations uses the npdes_id fallback when registry_id is
        # blank, so the streamer's permit_to_registry-backfill is
        # belt-and-suspenders.
        events.extend(sewer_events)

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
        candidates = _drilldown_candidates(leads, prior_scores,
                                           prior_eligibility=prior_eligibility)
        gated_unactionable = sum(
            1 for L in leads
            if L["lead_score"] >= EVENT_DRILLDOWN_MIN_SCORE
            and L["outreach_posture"] == "no_events"
            and _is_unactionable_for_drilldown(L)
        )
        drilldown_stats["candidates"] = len(candidates)
        drilldown_stats["gated_unactionable"] = gated_unactionable
        if gated_unactionable:
            log.info("Drill-down gating: %d high-value lead(s) excluded as "
                     "un-actionable (RNC-only / terminated / no-violation); "
                     "%d candidate(s) remain for fine-comb",
                     gated_unactionable, len(candidates))
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
                                       failed_out=failed_keys,
                                       prior_streaks=prior_streaks)
            sdwa_recovered = _drill_sdwa(candidates, events,
                                         inter_call_sleep=2.0, missed_out=None,
                                         failed_out=failed_keys,
                                         prior_streaks=prior_streaks)
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
        # Keys the gate excluded from fine-comb — surfaced as their own
        # bucket so the viewer can distinguish "we asked and EPA said
        # nothing" (no_data) from "we deliberately didn't ask" (gated).
        gated_keys = {(L["registry_id"], L["program"]) for L in high_value
                      if _is_unactionable_for_drilldown(L)}
        drilldown_stats.update(
            _health.summarize_drilldown(high_value, events, failed_keys,
                                        leads, gated_keys=gated_keys))

    # 5. Persist to DB. snapshot.sqlite is the source of truth — the
    # standing-inventory CSVs (all_leads, violation_events) and the
    # new_* diff CSVs are now materialized on demand by
    # `python -m chemtreat_water_leads.dump_run` rather than written
    # every run. Only `newly_snc_*.csv` (needs the prior snc_flag the
    # upsert overwrites) and `run_health.json` (captures run-time
    # warnings) land in the run folder — both are irrecoverable later.
    run_start_ts = datetime.utcnow().isoformat(timespec="seconds")
    run_dir = _run_output_dir(out_dir, "bulk", states, run_start_ts)
    with snapshot.open_db(db_path) as conn:
        # record_run first so its returned run_id can be threaded through
        # both upserts — every touched row gets a (run_id, key) entry in
        # the run_*_membership tables. Same transaction, so a mid-run
        # failure rolls back the run row too.
        notes = f"bulk_loader{' --no-events' if not include_events else ''}"
        run_id = snapshot.record_run(conn, notes=notes, now=run_start_ts)
        fac_diff = snapshot.diff_and_upsert_facilities(
            conn, leads, run_id, now=run_start_ts)
        viol_diff = snapshot.diff_and_upsert_violations(
            conn, events, run_id, now=run_start_ts)
    today = datetime.utcnow().strftime("%Y%m%d")
    _write_csv(run_dir / f"newly_snc_{today}.csv", fac_diff["newly_snc"])

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
    log.info("Run %d outputs in %s (run_health.json, newly_snc_*.csv). "
             "To materialize all_leads.csv + violation_events.csv for the "
             "viewer, run:  python -m chemtreat_water_leads.dump_run "
             "--db %s --run-id %d --out ./materialized/run_%d",
             run_id, run_dir, db_path, run_id, run_id)
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
