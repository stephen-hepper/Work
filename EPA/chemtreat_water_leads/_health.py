"""Run-health snapshot writer.

Both `bulk_loader.run_bulk` and `pipeline.run` call `write_run_health`
at end of run to emit `out/run_health.json`. The viewer's "Run Health"
tab consumes this file and surfaces signals (high-score leads with no
event detail, per-state concentration of those, depth-gap counts, run
warnings, suggested next commands) to non-technical users who never
look at terminal output.

Kept as a tiny standalone module so both entry points can use it
without creating an import cycle between `bulk_loader` and `pipeline`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path


SCHEMA_VERSION = 2


class WarningCollector(logging.Handler):
    """Capture WARNING-and-above records into a list for later dump.

    Install on the `chemtreat` logger tree at the start of a run,
    remove in a try/finally so it doesn't leak into other code that
    shares the root logger (e.g. tests).
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            self.records.append({
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except Exception:
            pass


def per_state_missing_events(leads: list[dict], min_score: int) -> dict[str, int]:
    """Count high-score leads with no_events posture, grouped by state.
    Drives the viewer's 'suggested API top-up territories' panel.
    Returned dict is sorted descending by count for stable display."""
    out: dict[str, int] = {}
    for L in leads:
        if (L.get("lead_score") or 0) < min_score:
            continue
        if L.get("outreach_posture") != "no_events":
            continue
        st = L.get("state") or "?"
        out[st] = out.get(st, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def summarize_drilldown(high_value: list[dict], events: list[dict],
                        failed_keys: set, leads: list[dict]) -> dict:
    """Classify the high-value drill-down outcomes for the Run Health tab.

    Splits the leads that were *attempted* (score >= threshold, with the
    identifier their program needs) into three buckets:

      with_events   - drilled and found ≥1 event
      lookup_failed - drill RAISED (timeout / connection drop / bot-block);
                      incomplete, worth re-running. `failed_keys` carries
                      these (final-attempt outcome) from the drill helpers.
      no_data       - drilled but returned no rows. Usually legitimate
                      (reporting-only or stormwater general-permit
                      noncompliance has no effluent exceedances to return);
                      occasionally a silently-throttled HTTP-200-empty that
                      can't be distinguished from real emptiness here.

    Returns counts, the per-state breakdown of the lookup_failed bucket
    (so the viewer can build a targeted re-run command), and the explicit
    failed keys as "registry_id|program" strings (so the viewer can mark
    those leads' posture as "lookup failed" vs. "no records on file").
    """
    attempted = {
        (L["registry_id"], L["program"]) for L in high_value
        if (L["program"] == "CWA" and L.get("permit_id"))
        or (L["program"] == "SDWA" and L.get("registry_id"))
    }
    with_events = {
        (e.get("registry_id"), e.get("program")) for e in events
    } & attempted
    no_event = attempted - with_events
    failed = no_event & set(failed_keys)
    no_data = no_event - failed

    state_by_key = {
        (L["registry_id"], L["program"]): (L.get("state") or "?") for L in leads
    }
    by_state: dict[str, int] = {}
    for k in failed:
        st = state_by_key.get(k, "?")
        by_state[st] = by_state.get(st, 0) + 1

    return {
        "attempted": len(attempted),
        "with_events": len(with_events),
        "lookup_failed": len(failed),
        "no_data": len(no_data),
        "lookup_failed_by_state": dict(
            sorted(by_state.items(), key=lambda kv: -kv[1])
        ),
        "lookup_failed_keys": sorted(f"{k[0]}|{k[1]}" for k in failed),
    }


def count_cwa_events_with_dmr_detail(events: list[dict]) -> tuple[int, int]:
    """Return (events_with_full_dmr, total_cwa_events).

    Bulk NPDES violation CSVs don't carry parameter/limit_value; only
    the API's `get_effluent_chart` does. The ratio tells the viewer
    how much of the CWA event inventory has actionable per-DMR depth
    vs. just violation_code/description.
    """
    total = 0
    with_detail = 0
    for e in events:
        if e.get("program") != "CWA":
            continue
        total += 1
        if e.get("parameter") and e.get("limit_value") not in (None, ""):
            with_detail += 1
    return with_detail, total


def write_run_health(out_dir: Path, *,
                     command: str,
                     states: list[str] | None,
                     include_events: bool,
                     run_start_ts: str,
                     leads: list[dict],
                     events: list[dict],
                     fac_diff: dict,
                     viol_diff: dict,
                     drilldown_stats: dict | None,
                     warnings: list[dict],
                     event_drilldown_min_score: int,
                     secondary_drilldown_min_score: int) -> Path:
    """Write `out/run_health.json` and return the path.

    Schema version is `SCHEMA_VERSION` (currently 2). Bump it if keys
    change; the viewer's `renderHealth()` refuses to render unknown
    versions.
    """
    leads_cwa = sum(1 for L in leads if L.get("program") == "CWA")
    leads_sdwa = sum(1 for L in leads if L.get("program") == "SDWA")
    dmr_with_detail, dmr_total = count_cwa_events_with_dmr_detail(events)
    high_no_events_by_state = per_state_missing_events(
        leads, event_drilldown_min_score
    )

    health = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": run_start_ts,
        "command": command,
        "states_filter": states,
        "include_events": include_events,
        "totals": {
            "leads": len(leads),
            "leads_cwa": leads_cwa,
            "leads_sdwa": leads_sdwa,
            "events": len(events),
            "new_facilities": len(fac_diff.get("new") or []),
            "newly_snc": len(fac_diff.get("newly_snc") or []),
            "new_violations": len(viol_diff.get("new") or []),
        },
        "drilldown": drilldown_stats or {},
        "high_score_no_events_by_state": high_no_events_by_state,
        "depth": {
            "cwa_events_with_dmr_detail": dmr_with_detail,
            "cwa_events_total": dmr_total,
            "sdwa_gate": ("tight (SNC + formal-action)"
                          if command == "bulk_loader"
                          else "broad (p_viola=Y)"),
        },
        "thresholds": {
            "event_drilldown_min_score": event_drilldown_min_score,
            "secondary_drilldown_min_score": secondary_drilldown_min_score,
        },
        "warnings": warnings,
        "lag_notice": (
            "SDWA reporting lag ~90 days; CWA DMR lag ~30-45 days. "
            "Verify status before outreach."
        ),
    }
    path = out_dir / "run_health.json"
    path.write_text(json.dumps(health, indent=2, default=str))
    return path
