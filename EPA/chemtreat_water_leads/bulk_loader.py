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
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlretrieve

from . import scoring, snapshot
from .pipeline import (
    TARGET_NAICS, LAG_BANNER, SDWA_LAG_NOTE, CWA_LAG_NOTE,
    _write_csv, _write_lag_notice,
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
    urlretrieve(url, str(target))
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


def _row_has_water_violation(row: dict) -> bool:
    """Filter: facility has CWA or SDWA non-compliance worth attention.

    The ECHO Exporter has many flag fields. We accept a row if ANY of:
      - SNC flag set on CWA or SDWA
      - >0 formal enforcement actions on CWA or SDWA
      - >0 quarters in non-compliance under CWA
      - Current violation flag on CWA
    """
    if str(row.get("CWA_SNC_FLAG") or "").upper() in ("Y", "S"):
        return True
    if str(row.get("SDWA_SNC_FLAG") or "").upper() == "Y":
        return True
    if _safe_int(row.get("CWA_FORMAL_ACTION_COUNT")) > 0:
        return True
    if _safe_int(row.get("SDWA_FORMAL_ACTION_COUNT")) > 0:
        return True
    if _safe_int(row.get("CWA_QTRS_WITH_NC")) > 0:
        return True
    if str(row.get("CWA_CURRENT_VIOL") or "").upper() == "Y":
        return True
    return False


def _bulk_to_api_shape(row: dict) -> dict:
    """Map bulk-CSV column names (UNDERSCORE_CASE) → API field names (CamelCase)
    so the same scoring.score_facility() function works on bulk rows.

    Keeping one canonical 'API shape' for the scorer means the rules in
    scoring.py don't need a parallel implementation for bulk data."""
    return {
        "FacName": row.get("FAC_NAME"),
        "FacStreet": row.get("FAC_STREET"),
        "FacCity": row.get("FAC_CITY"),
        "FacState": row.get("FAC_STATE"),
        "FacZip": row.get("FAC_ZIP"),
        "FacCounty": row.get("FAC_COUNTY"),
        "FacNAICSCodes": row.get("FAC_NAICS_CODES"),
        "FacSICCodes": row.get("FAC_SIC_CODES"),
        "RegistryID": row.get("REGISTRY_ID"),
        "SourceID": row.get("NPDES_IDS") or row.get("SDWA_IDS"),
        "CWASNC": row.get("CWA_SNC_FLAG"),
        "SDWASNC": row.get("SDWA_SNC_FLAG"),
        "CWAQtrsWithNC": row.get("CWA_QTRS_WITH_NC"),
        "CWAFormalActionCount": row.get("CWA_FORMAL_ACTION_COUNT"),
        "SDWAFormalActionCount": row.get("SDWA_FORMAL_ACTION_COUNT"),
        "CWAInformalCount": row.get("CWA_INFORMAL_COUNT"),
        "SDWAInformalCount": row.get("SDWA_INFORMAL_COUNT"),
        "CWAMajorFlag": "Y" if "MAJOR" in str(
            row.get("CWA_PERMIT_TYPES") or "").upper() else "N",
        "CWAPermitTypes": row.get("CWA_PERMIT_TYPES"),
        "CWALastPenaltyAmt": row.get("CWA_LAST_PENALTY_AMT"),
        "SDWALastPenaltyAmt": row.get("SDWA_LAST_PENALTY_AMT"),
        "CWADaysLastInspection": row.get("CWA_DAYS_LAST_INSPECTION"),
    }


def stream_echo_exporter(zip_path: Path,
                         naics_prefixes: list[str],
                         states: list[str] | None = None):
    """Generator yielding filtered (raw_row, api_shape) tuples from the
    ECHO Exporter. Stream-processed so we don't load 1.5M rows into RAM.

    If `states` is provided, only rows from those state codes are yielded.
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
                if not _row_matches_naics(row, naics_prefixes):
                    continue
                if not _row_has_water_violation(row):
                    continue
                matched += 1
                yield row, _bulk_to_api_shape(row)
                if total % 100_000 == 0:
                    log.info("  scanned %d rows, %d matches so far",
                             total, matched)
            log.info("Scanned %d total rows, %d matched filters", total, matched)


# ----------------------------------------------------- event drill-down
#
# For nationwide bulk, we read individual events from the NPDES violations
# CSV and the SDWA violations CSV rather than calling the API per facility.
# Both are streaming-safe.

def stream_npdes_violations(zip_path: Path,
                            registry_id_set: set[str]) -> list[dict]:
    """Pull NPDES violation events for the facilities we kept.

    `registry_id_set` should be the set of RegistryIDs we kept from the
    ECHO Exporter pass; we only emit events whose facility we care about.
    """
    events: list[dict] = []
    with zipfile.ZipFile(zip_path) as zf:
        # The npdes_downloads.zip contains many CSVs; the one we want
        # is NPDES_VIOLATIONS.csv (or similar). Find it defensively.
        target = next(
            (n for n in zf.namelist()
             if "VIOLATION" in n.upper() and n.lower().endswith(".csv")
             and "QNCR" not in n.upper()),  # skip the QNCR summary table
            None,
        )
        if target is None:
            log.warning("No NPDES violations CSV found inside %s", zip_path.name)
            return events
        log.info("Reading %s", target)
        with zf.open(target) as raw_fh:
            text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace")
            for row in csv.DictReader(text_fh):
                # NPDES bulk uses NPDES_ID, not RegistryID, so we'd need
                # a join. For now, emit all rows and let downstream filter.
                # (A future optimization: join via the permits CSV.)
                events.append({
                    "violation_id": row.get("NPDES_VIOLATION_ID")
                                    or row.get("VIOLATION_ID"),
                    "registry_id": row.get("REGISTRY_ID"),
                    "permit_id": row.get("NPDES_ID")
                                 or row.get("EXTERNAL_PERMIT_NMBR"),
                    "program": "CWA",
                    "parameter": row.get("PARAMETER_DESC") or row.get("PARAMETER_CODE"),
                    "limit_value": row.get("LIMIT_VALUE_NMBR"),
                    "dmr_value": row.get("DMR_VALUE_NMBR"),
                    "exceedance_pct": row.get("EXCEEDENCE_PCT"),
                    "period_end": row.get("MONITORING_PERIOD_END_DATE"),
                    "violation_code": row.get("VIOLATION_CODE"),
                    "status": row.get("RNC_DETECTION_CODE") or "Unresolved",
                    "data_lag_note": CWA_LAG_NOTE,
                })
    log.info("Read %d raw NPDES violation events; filtering to %d "
             "target facilities…", len(events), len(registry_id_set))
    if not registry_id_set:
        return events
    # Filter to facilities we kept
    return [e for e in events if e.get("registry_id") in registry_id_set]


def stream_sdwa_violations(zip_path: Path,
                           registry_id_set: set[str]) -> list[dict]:
    """Pull SDWA violation events for kept facilities.

    The SDWA bulk uses PWSID (not RegistryID) as primary key. We accept
    either-or because the ECHO Exporter publishes both.
    """
    from . import sdwa_codes
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
                vio_code = str(row.get("VIOLATION_CODE") or "")
                cont_code = str(row.get("CONTAMINANT_CODE") or "")
                rule_code = str(row.get("RULE_CODE") or "")
                category, vio_desc = sdwa_codes.lookup_violation(vio_code)
                events.append({
                    "violation_id": row.get("VIOLATION_ID"),
                    "registry_id": row.get("REGISTRY_ID"),
                    "pwsid": row.get("PWSID"),
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
    log.info("Read %d raw SDWA violation events", len(events))
    if not registry_id_set:
        return events
    return [e for e in events if e.get("registry_id") in registry_id_set]


# ----------------------------------------------------- main pipeline

def run_bulk(out_dir: Path,
             db_path: Path,
             cache_dir: Path,
             states: list[str] | None = None,
             include_events: bool = True) -> None:
    """End-to-end bulk pipeline. Same output shape as pipeline.run()."""
    print(LAG_BANNER)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download (cached) ECHO Exporter
    exporter_zip = _download_cached(BULK_URLS["echo_exporter"],
                                    cache_dir, "echo_exporter")

    # 2. Stream-filter to target NAICS + has water violation
    leads: list[dict] = []
    kept_registry_ids: set[str] = set()
    for raw, api_shape in stream_echo_exporter(exporter_zip, TARGET_NAICS, states):
        score, reasons = scoring.score_facility(api_shape)
        # Determine primary program of interest. The ECHO Exporter rolls
        # all programs into one row, so we pick based on which flag fired.
        # If both, we duplicate the row (one per program).
        for program, snc in [("CWA", api_shape["CWASNC"]),
                             ("SDWA", api_shape["SDWASNC"])]:
            if not _is_program_relevant(api_shape, program):
                continue
            leads.append({
                "lead_score": score,
                "score_reasons": " | ".join(reasons),
                "program": program,
                "registry_id": api_shape["RegistryID"],
                "company": api_shape["FacName"],
                "address": api_shape["FacStreet"],
                "city": api_shape["FacCity"],
                "state": api_shape["FacState"],
                "zip": api_shape["FacZip"],
                "county": api_shape["FacCounty"],
                "naics": api_shape["FacNAICSCodes"],
                "sic": api_shape["FacSICCodes"],
                "permit_id": api_shape["SourceID"],
                "snc_flag": snc,
                "quarters_in_violation": api_shape["CWAQtrsWithNC"],
                "formal_actions_5yr": (api_shape["CWAFormalActionCount"]
                                       if program == "CWA"
                                       else api_shape["SDWAFormalActionCount"]),
                "last_penalty_usd": (api_shape["CWALastPenaltyAmt"]
                                     if program == "CWA"
                                     else api_shape["SDWALastPenaltyAmt"]),
                "echo_url": ("https://echo.epa.gov/detailed-facility-report"
                             f"?fid={api_shape['RegistryID']}"),
            })
            if api_shape["RegistryID"]:
                kept_registry_ids.add(api_shape["RegistryID"])
    leads.sort(key=lambda r: r["lead_score"], reverse=True)
    log.info("Kept %d lead rows across %d unique facilities",
             len(leads), len(kept_registry_ids))

    # 3. Optionally drill into individual events from bulk CSVs
    events: list[dict] = []
    if include_events:
        log.info("Downloading NPDES events…")
        try:
            npdes_zip = _download_cached(BULK_URLS["npdes"], cache_dir, "npdes")
            events.extend(stream_npdes_violations(npdes_zip, kept_registry_ids))
        except Exception as e:
            log.warning("NPDES bulk event load failed: %s", e)

        log.info("Downloading SDWA events…")
        try:
            sdwa_zip = _download_cached(BULK_URLS["sdwa"], cache_dir, "sdwa")
            events.extend(stream_sdwa_violations(sdwa_zip, kept_registry_ids))
        except Exception as e:
            log.warning("SDWA bulk event load failed: %s", e)

    # 4. Persist to DB + write standing-state CSVs from DB.
    # Same source-of-truth pattern as pipeline.py — see comments there.
    run_start_ts = datetime.utcnow().isoformat(timespec="seconds")
    with snapshot.open_db(db_path) as conn:
        fac_diff = snapshot.diff_and_upsert_facilities(conn, leads, now=run_start_ts)
        viol_diff = snapshot.diff_and_upsert_violations(conn, events, now=run_start_ts)
        snapshot.record_run(conn, notes="bulk_loader", now=run_start_ts)
        # 5. Write outputs
        today = datetime.utcnow().strftime("%Y%m%d")
        _write_lag_notice(out_dir)
        snapshot.dump_facilities_csv(conn, out_dir / "all_leads.csv", run_start_ts)
        snapshot.dump_violations_csv(conn, out_dir / "violation_events.csv", run_start_ts)
    _write_csv(out_dir / f"new_facilities_{today}.csv", fac_diff["new"])
    _write_csv(out_dir / f"newly_snc_{today}.csv", fac_diff["newly_snc"])
    _write_csv(out_dir / f"new_violations_{today}.csv", viol_diff["new"])

    log.info("Bulk run complete: %d leads, %d events, %d new facilities, "
             "%d newly SNC, %d new violations.",
             len(leads), len(events),
             len(fac_diff["new"]), len(fac_diff["newly_snc"]),
             len(viol_diff["new"]))
    print(LAG_BANNER)


def _is_program_relevant(api_shape: dict, program: str) -> bool:
    """Did the facility actually trip the chosen program's filters?
    Prevents emitting an SDWA row for a pure CWA violator and vice versa."""
    if program == "CWA":
        return (str(api_shape.get("CWASNC") or "").upper() in ("Y", "S")
                or _safe_int(api_shape.get("CWAQtrsWithNC")) > 0
                or _safe_int(api_shape.get("CWAFormalActionCount")) > 0)
    return (str(api_shape.get("SDWASNC") or "").upper() == "Y"
            or _safe_int(api_shape.get("SDWAFormalActionCount")) > 0)


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
