# External Data Sources — Status Tracker

Running status of the EPA water-quality datasets surveyed in the
external-data review. Update the **Status** column as work lands; keep
**Notes** short (one line). For Tier-1 implementation detail, see
`EXTERNAL_DATA_PLAN.md`.

**Status legend:**
`not-started` · `planned` · `in-progress` · `shipped` · `deferred` · `wont-do`

---

## A note on "pre-violation" signals (rows 1 and 2)

The existing scoring rules (`rule_significant_violator`,
`rule_formal_action`, `rule_recent_penalty`, `rule_chronic_violation`)
all trigger on facilities that have already failed — SNC listings,
filed enforcement actions, paid penalties, repeated quarterly
non-compliance.

The new rules in rows 1 and 2 are different in kind. They fire on
**structural signals that exist independent of whether anything has
gone wrong yet**:

- **Permit covers our chemistry** — the facility's NPDES permit
  *lists* a treatable parameter (BOD, ammonia, phosphorus, TSS,
  oil/grease, metals, chlorine residual). They may be 100 % compliant
  today; the signal says "if they ever need help with this parameter,
  they're a buyer, because it's in their regulatory file."
- **Discharges to impaired water** — the outfall sits upstream of a
  303(d)-listed waterbody. Current permit may be fine, but the state
  is legally obligated to write a TMDL and tighten limits at the next
  renewal (~5 yr cycle). Parameter-match (+15) is stronger because
  the state has documented in writing that the facility's specific
  monitored parameter is a cause of the downstream impairment.

So "pre-violation" describes *the sales conversation*, not enforcement
status. A rep working from these signals leads with "we noticed your
permit covers phosphorus and the receiving water is on 303(d) for
nutrients — your limits are likely tightening at renewal, want to
talk now?" — account research, not opportunism.

---

## Tier 1 — High value, slot into `bulk_loader.py`

| # | Recommendation | Status | Notes |
|---|---|---|---|
| 1 | NPDES Permit Limits (`npdes_limits.zip`, 513 MB, weekly) | `shipped` | **Pre-violation signal** (see note above) — fires on *what the permit allows the facility to discharge*, not on whether they've violated. `rule_treatable_permit_parameter` (+5/hit, cap +15), `tag_treatable_permit`, `permit_has_*` + `permitted_parameters_text` columns. Live 2026-06-02 nationwide run: 9,101 CWA leads tagged (35 % of CWA inventory). |
| 2 | ATTAINS-NPDES Catchment (`npdes_attains_downloads.zip`, 103 MB, weekly) | `shipped` | **Pre-violation signal** (see note above) — fires on *regulatory trajectory of the receiving water*, not on whether the facility has violated. `rule_discharges_to_impaired` (+10 plain / +15 when E90 parameter match), `tag_discharges_to_impaired`, `tag_impairment_parameter_match`, `discharges_to_impaired` + `impairment_causes_text` + `matching_impaired_parameters` columns. Live run: 10,686 leads tagged (27 %); 650 with parameter match (1.6 %, all high-confidence). |
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
