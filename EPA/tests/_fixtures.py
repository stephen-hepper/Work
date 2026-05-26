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
