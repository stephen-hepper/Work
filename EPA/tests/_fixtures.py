"""Test fixture helpers for the bulk_loader suite.

We build small in-memory ECHO-Exporter and per-program violation zips
on disk in a tmp dir so the bulk_loader's `zipfile.ZipFile(path)` call
opens them like the real EPA downloads. No network access; the EPA
filename / CSV-header shapes are reproduced from the actual downloaded
files in `EPA/cache/`.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

# Minimal column set that satisfies _row_has_*_signal + _bulk_to_program_shapes.
# Add more columns to a given fixture row as the test requires.
EXPORTER_HEADER = [
    "REGISTRY_ID", "FAC_NAME", "FAC_STREET", "FAC_CITY", "FAC_STATE",
    "FAC_ZIP", "FAC_COUNTY", "FAC_NAICS_CODES", "FAC_SIC_CODES",
    "NPDES_IDS", "SDWA_IDS",
    "CWA_SNC_FLAG", "CWA_FORMAL_ACTION_COUNT", "CWA_INFORMAL_COUNT",
    "CWA_QTRS_WITH_NC", "CWA_CURRENT_VIOL", "CWA_COMPLIANCE_STATUS",
    "CWA_PERMIT_TYPES", "CWA_LAST_PENALTY_AMT", "CWA_DAYS_LAST_INSPECTION",
    "CWA_13QTRS_COMPL_HISTORY",
    "SDWA_SNC_FLAG", "SDWA_FORMAL_ACTION_COUNT", "SDWA_INFORMAL_COUNT",
    "SDWA_COMPLIANCE_STATUS",
]

NPDES_SE_HEADER = [
    "NPDES_ID", "NPDES_VIOLATION_ID", "VIOLATION_TYPE_CODE", "VIOLATION_CODE",
    "VIOLATION_DESC", "SINGLE_EVENT_VIOLATION_DATE", "SINGLE_EVENT_END_DATE",
    "RNC_DETECTION_CODE", "RNC_DETECTION_DESC", "RNC_DETECTION_DATE",
    "RNC_RESOLUTION_CODE", "RNC_RESOLUTION_DESC", "RNC_RESOLUTION_DATE",
]

SDWA_VIOLATION_HEADER = [
    "SUBMISSIONYEARQUARTER", "PWSID", "VIOLATION_ID", "FACILITY_ID",
    "NON_COMPL_PER_BEGIN_DATE", "NON_COMPL_PER_END_DATE",
    "VIOLATION_CODE", "VIOLATION_CATEGORY_CODE", "IS_HEALTH_BASED_IND",
    "CONTAMINANT_CODE", "VIOLATION_STATUS", "RULE_CODE", "RULE_FAMILY_CODE",
]


def _normalize(row: dict, header: list[str]) -> dict:
    """Ensure every header column is present in the dict (csv.DictWriter
    raises ValueError on missing keys)."""
    return {col: row.get(col, "") for col in header}


def _write_csv(zf: zipfile.ZipFile, name: str, header: list[str],
               rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    for r in rows:
        w.writerow(_normalize(r, header))
    zf.writestr(name, buf.getvalue())


def make_exporter_zip(tmp_path: Path, rows: list[dict],
                      name: str = "echo_exporter.zip") -> Path:
    """Build an ECHO_EXPORTER.csv inside a zip at `tmp_path/name`.

    Caller passes a list of dicts with whatever subset of `EXPORTER_HEADER`
    columns the test cares about — missing columns are blanked.
    """
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, "ECHO_EXPORTER.csv", EXPORTER_HEADER, rows)
    return path


def make_npdes_zip(tmp_path: Path, rows: list[dict],
                   filename: str = "NPDES_SE_VIOLATIONS.csv",
                   name: str = "npdes.zip") -> Path:
    """Build a single per-event NPDES violations CSV inside a zip."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, filename, NPDES_SE_HEADER, rows)
    return path


def make_sdwa_zip(tmp_path: Path, rows: list[dict],
                  name: str = "sdwa.zip") -> Path:
    """Build SDWA_VIOLATIONS_ENFORCEMENT.csv inside a zip."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, "SDWA_VIOLATIONS_ENFORCEMENT.csv",
                   SDWA_VIOLATION_HEADER, rows)
    return path


# Headers verified against the real npdes_limits.zip
# (NPDES_LIMITS.csv, 2026-05-30 refresh). The streamer reads only the
# columns below; the full schema has ~50 cols but the rest are
# numeric metadata we don't use.
PERMIT_LIMITS_HEADER = [
    "EXTERNAL_PERMIT_NMBR",
    "LIMIT_SET_STATUS_FLAG",
    "PARAMETER_CODE",
    "PARAMETER_DESC",
    "PERM_FEATURE_NMBR",
    "STATISTICAL_BASE_CODE",
]

# Headers verified against the real npdes_attains_downloads.zip
# (NPDES_ATTAINS_AU_SUMMARIES.csv, 2026-05-30 refresh).
ATTAINS_SUMMARY_HEADER = [
    "REGISTRY_ID",
    "ECHO_DFR_URL",
    "NPDES_ID",
    "REPORTINGCYCLE",
    "STATE",
    "ASSESSMENTUNITIDENTIFIER",
    "AU_URL",
    "ASSESSMENTUNITNAME",
    "WATER_CONDITION",
    "POT_IMP_PARAMETERS",
    "E90_POT_IMP_PARAMETERS",
    "DRINKINGWATER_USE",
    "ECOLOGICAL_USE",
    "FISHCONSUMPTION_USE",
    "RECREATION_USE",
    "OTHER_USE",
    "CAUSE_GROUPS_IMPAIRED",
]


def make_permit_limits_zip(tmp_path: Path, rows: list[dict],
                            name: str = "npdes_limits.zip") -> Path:
    """Build NPDES_LIMITS.csv inside a zip mimicking the real file shape."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, "NPDES_LIMITS.csv", PERMIT_LIMITS_HEADER, rows)
    return path


def make_attains_zip(tmp_path: Path, rows: list[dict],
                      name: str = "npdes_attains.zip") -> Path:
    """Build NPDES_ATTAINS_AU_SUMMARIES.csv inside a zip mimicking the
    real archive (we only emit the summaries CSV; the two larger
    catchment/AU files the real archive ships aren't read by our code)."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, "NPDES_ATTAINS_AU_SUMMARIES.csv",
                   ATTAINS_SUMMARY_HEADER, rows)
    return path


# Header verified against the real npdes_dmr_fy2026.zip / NPDES_DMRS_FY2026.csv,
# 2026-05-30 refresh. The streamer only reads the columns below; the file
# carries 56 cols total. NOTE: the column name is "EXCEEDENCE_PCT"
# (EPA typo, not "EXCEEDANCE"). Do not "correct" without re-verifying
# against a fresh download.
DMR_HEADER = [
    "EXTERNAL_PERMIT_NMBR",
    "PERM_FEATURE_NMBR",
    "PARAMETER_CODE",
    "PARAMETER_DESC",
    "LIMIT_VALUE_NMBR",
    "LIMIT_VALUE_STANDARD_UNITS",
    "LIMIT_UNIT_DESC",
    "STANDARD_UNIT_DESC",
    "DMR_VALUE_NMBR",
    "DMR_VALUE_STANDARD_UNITS",
    "DMR_UNIT_DESC",
    "EXCEEDENCE_PCT",
    "MONITORING_PERIOD_END_DATE",
    "NPDES_VIOLATION_ID",
    "VIOLATION_CODE",
    "STATISTICAL_BASE_TYPE_CODE",
]


def make_dmr_zip(tmp_path: Path, rows: list[dict],
                  fy: int = 2026,
                  name: str = "npdes_dmr.zip") -> Path:
    """Build NPDES_DMRS_FY<fy>.csv inside a zip mimicking the real
    DMR archive shape. Streamer matches on the NPDES_DMRS_FY prefix."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, f"NPDES_DMRS_FY{fy}.csv", DMR_HEADER, rows)
    return path


# Headers verified against the real current_sewer_overflow_and_
# collection_systems_tables.zip, 2026-06-15 refresh. Lowercase snake
# case (EPA's choice — different convention from the older CWA/SDWA
# files which use SCREAMING_SNAKE). The streamer reads only a subset
# of the 39-column events table; the fixture mirrors only what the
# streamer touches plus the join key.
SEWER_EVENTS_HEADER = [
    "sewer_overflow_bypass_event_key",
    "permit_identifier",
    "sewer_overflow_bypass_start_datetime",
    "sewer_overflow_bypass_end_datetime",
    "sewer_overflow_bypass_discharge_volume_gallons",
    "wet_weather_occurance_indicator",
    "sewer_overflow_structure_type_desc",
    "collection_system_population",
]

SEWER_TYPES_HEADER = [
    "sewer_overflow_bypass_event_key",
    "permit_identifier",
    "sewer_overflow_bypass_type_code",
    "sewer_overflow_bypass_type_desc",
    "sewer_overflow_bypass_type_code_sequence",
]


def make_sewer_overflow_zip(
    tmp_path: Path,
    events: list[dict],
    types: list[dict],
    name: str = "sewer_overflow.zip",
) -> Path:
    """Build a sewer-overflow zip mimicking the real EPA archive shape.

    Only the two CSVs the streamer reads are emitted; the other six
    CSVs in the real archive (causes, impacts, corrective_actions,
    receiving_waters, treatment_codes, collection_system_permits,
    columns_metadata) plus the ERD PDF are omitted — the streamer
    doesn't touch them in v1.

    Caller passes:
      events: rows for sewer_overflow_bypass_report_events.csv
      types:  rows for sewer_overflow_bypass_types.csv (one-to-many on
              sewer_overflow_bypass_event_key)
    """
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_csv(zf, "sewer_overflow_bypass_report_events.csv",
                   SEWER_EVENTS_HEADER, events)
        _write_csv(zf, "sewer_overflow_bypass_types.csv",
                   SEWER_TYPES_HEADER, types)
    return path
