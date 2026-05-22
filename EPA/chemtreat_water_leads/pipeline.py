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

from . import echo_client, scoring, snapshot

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


def run(states: list[str], out_dir: Path, db_path: Path) -> None:
    print(LAG_BANNER)

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

    # 2a. CWA effluent exceedances (per-permit, fast endpoint)
    cwa_drilled = 0
    for lead in high_value:
        if lead["program"] != "CWA" or not lead["permit_id"]:
            continue
        try:
            for ev in echo_client.fetch_npdes_violation_events(
                    lead["permit_id"], start, end):
                ev["registry_id"] = lead["registry_id"]
                ev["program"] = "CWA"
                ev["company"] = lead["company"]
                ev["status"] = "Unresolved"   # default; refine via DFR later
                ev["data_lag_note"] = CWA_LAG_NOTE
                events.append(ev)
            cwa_drilled += 1
            time.sleep(0.3)
        except Exception as e:
            log.warning("CWA event fetch failed for %s: %s", lead["permit_id"], e)
    log.info("Drilled %d CWA permits", cwa_drilled)

    # 2b. SDWA violation history (per-system, via the DFR endpoint)
    #
    # The DFR is slower than get_effluent_chart - it returns the full
    # cross-program facility report - so we only call it for high-scoring
    # SDWA systems. The bundled sdwa_codes module translates EPA's numeric
    # codes into human-readable categories before the rows land in `events`.
    sdwa_drilled = 0
    for lead in high_value:
        if lead["program"] != "SDWA" or not lead["registry_id"]:
            continue
        try:
            for ev in echo_client.fetch_sdwa_violation_events(lead["registry_id"]):
                ev["company"] = lead["company"]
                ev["data_lag_note"] = SDWA_LAG_NOTE
                events.append(ev)
            sdwa_drilled += 1
            time.sleep(0.5)   # DFR is heavier; back off a bit more
        except Exception as e:
            log.warning("SDWA event fetch failed for %s: %s",
                        lead["registry_id"], e)
    log.info("Drilled %d SDWA systems", sdwa_drilled)

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

    # ---- 3. Diff against last snapshot --------------------------------
    with snapshot.open_db(db_path) as conn:
        fac_diff = snapshot.diff_and_upsert_facilities(conn, leads)
        viol_diff = snapshot.diff_and_upsert_violations(conn, events)
        snapshot.record_run(conn, notes=f"states={','.join(states)}")

    # ---- 4. Write outputs ---------------------------------------------
    today = datetime.utcnow().strftime("%Y%m%d")
    _write_lag_notice(out_dir)
    _write_csv(out_dir / "all_leads.csv", leads)
    _write_csv(out_dir / "violation_events.csv", events)
    _write_csv(out_dir / f"new_facilities_{today}.csv", fac_diff["new"])
    _write_csv(out_dir / f"newly_snc_{today}.csv", fac_diff["newly_snc"])
    _write_csv(out_dir / f"new_violations_{today}.csv", viol_diff["new"])

    log.info("Done. %d new facilities, %d newly SNC, %d new violation events.",
             len(fac_diff["new"]), len(fac_diff["newly_snc"]),
             len(viol_diff["new"]))
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