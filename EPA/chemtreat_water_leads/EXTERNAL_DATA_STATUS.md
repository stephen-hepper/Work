# External Data Sources — Status Tracker

Running status of the EPA water-quality datasets surveyed in the
external-data review. Update the **Status** column as work lands; keep
**Notes** short (one line). For Tier-1 implementation detail, see
`EXTERNAL_DATA_PLAN.md`.

**Status legend:**
`not-started` · `planned` · `in-progress` · `shipped` · `deferred` · `wont-do`

---

## Tier 1 — High value, slot into `bulk_loader.py`

| # | Recommendation | Status | Notes |
|---|---|---|---|
| 1 | NPDES Permit Limits (`npdes_limits.zip`, 459 MB, weekly) | `planned` | Pre-violation signal. See EXTERNAL_DATA_PLAN.md. |
| 2 | ATTAINS-NPDES Catchment (`npdes_attains_downloads.zip`, 66 MB, weekly) | `planned` | Downstream-impairment signal. See EXTERNAL_DATA_PLAN.md. |
| 3 | NPDES Effluent Violations Part 2 + DMR archives | `not-started` | Closes the bulk CWA per-DMR detail gap (currently None for parameter/limit/dmr/exceedance). Could deprecate the API fine-comb for CWA depth. |
| 4 | Sewer Overflow / CSO / SSO events (daily refresh!) | `not-started` | POTW lead signal. The only EPA water dataset with daily cadence — collapses the 30–90d lag we have elsewhere. |
| 5 | TRI Surface Water Releases (annual, via Envirofacts API or POLL_RPT bulk) | `not-started` | Per-facility, per-chemical lb/yr to surface water + POTW transfers. Joins on FRS RegistryID. |

## Tier 2 — Strong fit, depends on product line

| # | Recommendation | Status | Notes |
|---|---|---|---|
| 6 | UCMR5 PFAS Occurrence (zipped text, PWSID-keyed) | `not-started` | Tier-1 if ChemTreat sells PFAS treatment chemistry — confirm with sales. PFOA/PFOS MCLs enforceable since 2024-04. |
| 7 | Industrial Stormwater MSGP AIM events | `not-started` | Niche — facilities forced into Additional Implementation Measures = mandatory stormwater treatment. |

## Tier 3 — Background enrichment, defer

| # | Recommendation | Status | Notes |
|---|---|---|---|
| 8 | Water Quality Portal (WQX/USGS ambient measurements) | `deferred` | 430M records but needs HUC/NHDPlus spatial joining. Revisit when a concrete request lands. |
| 9 | NPDES Biosolids (`npdes_biosolids_downloads.zip`) | `deferred` | Niche; only biosolids-handling facilities. |
| 10 | Facility Demographics (`echo_demographics.zip`, 567 MB) | `wont-do` | Environmental-justice context, not lead-gen. |
| 11 | FRS parent corporation / ownership rollup | `deferred` | CRM enrichment, not signal — punt until CRM integration is on the roadmap. |

---

## Update protocol

When implementing a row:

1. Move status `not-started` → `planned` (link to a plan doc).
2. `planned` → `in-progress` when work starts.
3. `in-progress` → `shipped` after merge AND a successful full run that
   produces sane non-zero hit rates (e.g. >5% of CWA leads tagged).
4. If a row is dropped after exploration, mark `wont-do` with a
   one-line reason — leave it in the table so the next reviewer
   doesn't re-propose it.

---

## Quick reference — verifying any URL still works

EPA renames bulk files occasionally (MEMORY.md trap). If a download
404s, the canonical catalog is at:

<https://echo.epa.gov/tools/data-downloads>

Update `BULK_URLS` in `bulk_loader.py` if file names have shifted.
