# Data Description

The full catalog of EPA datasets we aggregate, with refresh cadence and
reporting lag for each. For acronym definitions
(CWA, SDWA, NPDES, DMR, DFR, ATTAINS, 303(d), TMDL, PWS, FRS, ICIS-NPDES,
SDWIS-Fed, FY, NAICS, etc.) see the **Acronyms you'll see** section of
[`STARTING_GUIDE.md`](STARTING_GUIDE.md).

All sources come from EPA (no API key required, no auth). Two ingest paths.

---

## API path — `echodata.epa.gov`

Lives in `echo_client.py`. Used by `pipeline.py` for per-state runs and by
`bulk_loader.py`'s API fine-comb fallback on high-value /
newly-discovered / score-jumped leads.

| Endpoint | Backed by | What we get | Refresh cadence | Reporting lag |
|---|---|---|---|---|
| `cwa_rest_services.get_facilities` (+`get_qid`) | ICIS-NPDES | Active CWA dischargers with compliance flags | Live query (state-of-the-DB) | CWA DMR data ~30–45 days |
| `sdw_rest_services.get_systems` (+`get_qid`) | SDWIS-Fed | Public water systems with violations + the PWS metadata (`PopulationServedCount`, `PWSTypeDesc`, `OwnerDesc`, `PrimarySourceDesc`) | Live query | SDWA reporting ~90 days |
| `eff_rest_services.get_effluent_chart` | ICIS-NPDES DMRs | Per-DMR exceedance events for one NPDES permit (parameter, limit, measured, exceedance %) | Live query | ~30–45 days |
| `dfr_rest_services.get_dfr` | ICIS / SDWIS / FRS composite | Per-facility drill-down. SDWA violations live under `Results.ViolationsEnforcementActions.Sources[*].Violations` (see MEMORY.md trap #10). | Live query | Mixed (CWA 30–45d, SDWA ~90d) |

---

## Bulk path — `echo.epa.gov/files/echodownloads/`

Lives in `bulk_loader.BULK_URLS`. Six weekly-refreshed zips, ~2.2 GB
compressed total. None of the unzipped data ever lands in memory whole —
every streamer uses `csv.DictReader` over a `zipfile.open()` handle.

| Zip | Compressed | Unzipped | What it produces | Refresh cadence | Reporting lag |
|---|---|---|---|---|---|
| `echo_exporter.zip` | ~423 MB | ~250 MB CSV | 1.5M facilities × 130+ cols — inventory + summary compliance flags | Weekly | Underlying lag (CWA 30–45d, SDWA ~90d) |
| `npdes_downloads.zip` | ~327 MB | varies | NPDES SE/PS/CS violation events (codes + dates, no per-DMR detail) | Weekly | ~30–45 days |
| `SDWA_latest_downloads.zip` | ~499 MB | varies | Individual SDWA violation events | **Quarterly** (per bulk_loader.py docstring) | ~90 days |
| `npdes_limits.zip` | ~490 MB | ~7.2 GB | NPDES permit limits — what each permit *allows* (powers `permit_has_*` and `rule_treatable_permit_parameter`) | Weekly | Permits move slowly (~5-yr renewal cycle) so lag isn't meaningful here |
| `npdes_attains_downloads.zip` | ~99 MB | ~570 MB | NPDES↔ATTAINS 303(d) catchment linkage (powers `discharges_to_impaired` / `matching_impaired_parameters` / `rule_discharges_to_impaired`) | Weekly | ATTAINS assessments turn over on state cycles, typically every 2 yr (federal Integrated Reporting cycle) |
| `npdes_dmrs_fy2026.zip` | ~344 MB | ~5 GB | Per-DMR submissions with `EXCEEDENCE_PCT` (CWA per-DMR depth; powers `top_exceedance_pct`, `rule_recent_dmr_exceedance`, `rule_exceeds_treatable_parameter`) | Weekly for current FY; older FYs frozen | ~30–45 days |

**Local cache:** Downloaded zips are kept in `--cache` for 7 days (matches
EPA's weekly cadence). After 7 days the next run re-downloads.

**Bulk path's NPDES SE / PS / CS files:** `npdes_downloads.zip` contains
three sibling CSVs we read together — Single-Event effluent exceedances
(`NPDES_SE_VIOLATIONS.csv`), Permit-Schedule milestones (`NPDES_PS_*`), and
Compliance-Schedule events (`NPDES_CS_*`). See `RATIONALE.md` "Bulk NPDES
violation file selection" for why we don't read `NPDES_VIOLATION_ENFORCEMENTS.csv`.

---

## Lag is surfaced in five places

A "newly seen" SDWA violation in today's diff may actually have happened
~3 months ago and already been resolved on the ground. To make this hard
to miss for sales:

1. **Console banner** printed at the start *and end* of every run.
2. **`data_lag_note` column** on every event row in `violation_events.csv`.
3. **`READ_ME_FIRST.txt`** dropped into every output folder.
4. **README.md "Reporting lag" section** with the per-program lag breakdown.
5. **Sticky yellow banner** at the top of the viewer page that can't be dismissed.

---

## What's queued but not yet aggregated

Tracked in `EXTERNAL_DATA_STATUS.md`. Status as of 2026-06:

| # | Source | Tier | Why we want it | Status |
|---|---|---|---|---|
| 4 | Sewer Overflow / CSO / SSO events | Tier-1 | **Daily** refresh cadence — the only EPA water dataset that collapses the 30–90d lag. POTW lead signal. | Not started |
| 5 | TRI Surface Water Releases | Tier-1 | Annual per-facility per-chemical lb/yr to surface water + POTW transfers. Joins on FRS RegistryID. | Not started |
| 6 | UCMR5 PFAS Occurrence | Tier-2 | PWSID-keyed. Tier-1 if ChemTreat sells PFAS treatment chemistry — pending sales confirmation. PFOA/PFOS MCLs enforceable since 2024-04. | Not started |
| 7 | Industrial Stormwater MSGP AIM events | Tier-2 | Niche — facilities forced into Additional Implementation Measures = mandatory stormwater treatment. | Not started |

**Deferred** (Tier 3): Water Quality Portal (WQX/USGS — needs HUC spatial joining),
NPDES Biosolids, FRS parent-corporation rollup. **Won't do**: Facility
Demographics (environmental-justice context, not lead-gen).

---

## See also

- [`STARTING_GUIDE.md`](STARTING_GUIDE.md) — acronym glossary, sales-facing first-run walkthrough
- [`README.md`](README.md) — methodology, scoring rules, caveats
- [`MEMORY.md`](MEMORY.md) — the silent-failure trail behind the data paths (read before touching `echo_client.py`)
- [`EXTERNAL_DATA_STATUS.md`](EXTERNAL_DATA_STATUS.md) — full integration roadmap with rationale per source
- [`RATIONALE.md`](RATIONALE.md) — design notes for the bulk vs API split, per-program shapes, drill-down triggers
- [`COMMANDS.md`](COMMANDS.md) — practical run patterns and time estimates
