# External Data Integration Plan — Permit Limits + ATTAINS

Implementation plan for the two Tier-1 data-source adds from the
external-data survey (see `EXTERNAL_DATA_STATUS.md` for the full
roadmap). These two together turn the tool from "leads that already
violated" into "leads set up to violate the parameters ChemTreat treats,
discharging into already-impaired waters."

---

## Goal

Add two EPA bulk feeds to `bulk_loader.py` so every lead row carries:

1. **Permitted-parameter signals** — which ChemTreat-treatable pollutants
   the facility's NPDES permit allows it to discharge (pre-violation
   signal: facility is *set up* to need treatment).
2. **Impaired-water signals** — whether the facility discharges into a
   waterbody on the 303(d) impaired list (state will tighten the permit;
   facility needs a remediation plan).

Both fire in pass-1 scoring, so they affect drill-down candidate
selection — not just final rank.

---

## Datasets

### 1. NPDES Permit Limits

| Field | Value |
|---|---|
| URL | `https://echo.epa.gov/files/echodownloads/npdes_limits.zip` |
| Size | ~459 MB |
| Refresh | Weekly (matches our existing 7-day cache window) |
| Join key | `NPDES_ID` (already in `kept_npdes_permits` after `stream_echo_exporter`) |

Expected columns (verify on first download — names follow ICIS-NPDES
conventions; treat docs as 80% right per MEMORY.md):
`NPDES_ID`, `PERM_FEATURE_NMBR` (outfall), `PARAMETER_CODE`,
`PARAMETER_DESC`, `LIMIT_VALUE_NMBR`, `LIMIT_UNIT_CODE`,
`STAT_BASE_TYPE_CODE`, `MONITORING_LOCATION_CODE`, `SEASON_NUM`.

One permit → many limits (one row per outfall × parameter × statistic
basis × season). We roll up to **facility level**, boolean per
ChemTreat-treatable parameter class. Per-outfall detail stays in the
zip if a future rep needs it.

### 2. ATTAINS-NPDES Catchment Linkage

| Field | Value |
|---|---|
| URL | `https://echo.epa.gov/files/echodownloads/npdes_attains_downloads.zip` |
| Size | ~66 MB |
| Refresh | Weekly |
| Join key | `NPDES_ID` → `ASSESSMENT_UNIT_ID` → impairment causes |

EPA has pre-computed the NHDPlus spatial join — we do **not** need to
do reach-tracing ourselves. Likely two CSVs inside:

- A linkage table: `NPDES_ID`, `ASSESSMENT_UNIT_ID`, distance / catchment ID.
- An impairments table: `ASSESSMENT_UNIT_ID`, `CAUSE_NAME` (parameter
  driving the impairment), `IMPAIRMENT_CATEGORY`.

Verify file names on first download; current selector pattern in
`stream_sdwa_violations` (substring match on `"VIOLATION"`) generalises.

---

## Schema additions (`snapshot.py`)

Append to `FAC_COLUMNS` (auto-migrates via `_migrate(conn)`):

```python
# --- Permit-limit signals (rolled up from npdes_limits.zip) ---
("permit_has_phosphorus",       "INTEGER"),
("permit_has_ammonia",          "INTEGER"),
("permit_has_tss",              "INTEGER"),
("permit_has_bod",              "INTEGER"),
("permit_has_oil_grease",       "INTEGER"),
("permit_has_metals",           "INTEGER"),
("permit_has_chlorine_residual","INTEGER"),
("permitted_parameters_text",   "TEXT"),   # pipe-joined, top-N for the viewer

# --- ATTAINS impaired-water signals ---
("discharges_to_impaired",      "INTEGER"),
("impairment_causes_text",      "TEXT"),   # pipe-joined cause names
```

Plus matching entries in `FAC_CSV_COLUMNS` so `dump_facilities_csv`
emits them. The viewer renders the new tags via the existing `tag_*`
loop; no viewer column work needed for boolean signals.

No new table — the wide-row pattern keeps the viewer simple. If sales
later wants per-outfall detail, that's a separate `permit_outfalls`
table.

---

## Scoring (`scoring.py`)

Two new facility rules. Add to `RULES` after `rule_recent_inspection`:

```python
TREATABLE_PARAM_PATTERNS = {
    "phosphorus":        ("PHOSPHORUS",),
    "ammonia":           ("AMMONIA",),
    "tss":               ("TOTAL SUSPENDED", "TSS"),
    "bod":               ("BOD", "BIOCHEMICAL OXYGEN"),
    "oil_grease":        ("OIL AND GREASE", "OIL & GREASE"),
    "metals":            ("COPPER", "LEAD", "ZINC", "NICKEL",
                          "CHROMIUM", "CADMIUM"),
    "chlorine_residual": ("CHLORINE, TOTAL RESIDUAL",
                          "TOTAL RESIDUAL CHLORINE"),
}


def rule_treatable_permit_parameter(f, events=None):
    """+5 per ChemTreat-treatable parameter on permit, cap +15.

    Pre-violation lead signal: the permit ALLOWS the facility to
    discharge something we treat. Sales call doesn't depend on a
    current violation — it's an account-research signal."""
    cols = ("permit_has_phosphorus", "permit_has_ammonia",
            "permit_has_tss", "permit_has_bod",
            "permit_has_oil_grease", "permit_has_metals",
            "permit_has_chlorine_residual")
    hits = sum(1 for c in cols if f.get(c))
    if not hits:
        return None
    pts = min(hits * 5, 15)
    return pts, f"{hits} treatable parameter(s) on NPDES permit"


def rule_discharges_to_impaired(f, events=None):
    """+10 if facility discharges to a 303(d) impaired waterbody.

    Trajectory signal: state is required to write a TMDL and will
    tighten the permit. Facility will need a remediation plan."""
    if not f.get("discharges_to_impaired"):
        return None
    return 10, "Discharges to 303(d) impaired waterbody"
```

These run in **pass 1** (no events needed), so a facility that scores
+25 from these two rules alone clears `EVENT_DRILLDOWN_MIN_SCORE=50`
when combined with even a modest SNC/chronic signal. Pre-violation
leads now get drilled.

### Tags (`compute_tags`)

Append:

```python
"tag_treatable_permit":
    any(f.get(c) for c in PERMIT_HAS_COLS),
"tag_discharges_to_impaired":
    bool(f.get("discharges_to_impaired")),
```

Extend `tag_chemtreat_high_relevance` composite to include
`tag_treatable_permit OR tag_discharges_to_impaired` on the OR side
(while keeping `NOT tag_only_resolved_events` on the AND side).

---

## Pipeline wiring (`bulk_loader.py`)

### `BULK_URLS` additions

```python
BULK_URLS = {
    "echo_exporter": ...,
    "npdes":         ...,
    "sdwa":          ...,
    "npdes_limits":  "https://echo.epa.gov/files/echodownloads/npdes_limits.zip",
    "npdes_attains": "https://echo.epa.gov/files/echodownloads/npdes_attains_downloads.zip",
}
```

### New stream readers

```python
def stream_permit_limits(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Return {npdes_id: {permit_has_*: bool, ...,
                          permitted_parameters_text: str}}.

    Streams the limits CSV, filters to kept permits, classifies each
    PARAMETER_DESC into a ChemTreat-treatable bucket via substring
    match, and rolls up to one row per NPDES_ID."""

def stream_attains_linkage(
    zip_path: Path,
    kept_npdes_permits: set[str],
) -> dict[str, dict]:
    """Return {npdes_id: {discharges_to_impaired: bool,
                          impairment_causes_text: str}}.

    Streams the linkage CSV + impairment CSV, joins in-memory (66MB
    file → fits), filters to kept permits."""
```

Both follow the existing `stream_sdwa_violations` pattern: open zip,
pick CSV by name, `csv.DictReader`, filter by ID set, return a list /
dict. No pandas.

### `run_bulk` insertion point

Hook the augmentation **between** the existing exporter loop and the
initial `compute_tags`/sort block, so the new columns are present
before pass-1 scoring runs:

```python
# After stream_echo_exporter loop and lead construction
# (before the existing leads.sort and compute_tags pass):

if include_events:   # gated like the event downloads — no network in --no-events
    try:
        limits_zip = _download_cached(BULK_URLS["npdes_limits"],
                                       cache_dir, "npdes_limits")
        permit_signals = stream_permit_limits(limits_zip, kept_npdes_permits)
        for lead in leads:
            if lead["program"] != "CWA":
                continue
            sig = permit_signals.get(lead["permit_id"])
            if sig:
                lead.update(sig)
    except Exception as e:
        log.warning("Permit limits load failed: %s", e)

    try:
        attains_zip = _download_cached(BULK_URLS["npdes_attains"],
                                        cache_dir, "npdes_attains")
        impaired_signals = stream_attains_linkage(attains_zip, kept_npdes_permits)
        for lead in leads:
            if lead["program"] != "CWA":
                continue
            sig = impaired_signals.get(lead["permit_id"])
            if sig:
                lead.update(sig)
    except Exception as e:
        log.warning("ATTAINS linkage load failed: %s", e)

    # Re-score pass-1 so the new rules contribute to drill-down candidate
    # selection. _augment_leads with events=[] is the cheapest way.
    _augment_leads(leads, events=[])
    leads.sort(key=lambda r: r["lead_score"], reverse=True)
```

**Why gate behind `include_events`:** the `--no-events` contract is
"zero downloads, fully offline" (per `tests/test_no_events_flag.py`).
Permit limits and ATTAINS are new downloads, so they belong in the
same gate. If sales later wants pre-violation signals in air-gapped
mode, add a separate `--permit-limits-only` flag.

### Pipeline (API path) parity

`pipeline.run` currently uses `find_cwa_violators` which doesn't pull
permit limits or ATTAINS. Two options:

- **A.** Skip — permit-limit signal is a bulk-only feature. Document
  in SCORING_GUIDE.md that pipeline runs don't fire these rules.
- **B.** Read the permit-limits zip in pipeline.run too. Cheap because
  the zip is cached.

Recommendation: **A** for v1 (keep the API path minimal, fast), revisit
if sales asks. The bulk loader is already the recommended path for
nationwide and most regional runs (per `COMMANDS.md`).

---

## Tests

New tests under `tests/` matching existing patterns
(`tests/_fixtures.py` for in-memory zips):

- `test_permit_limits.py`
  - Synthetic zip with 3 NPDES_IDs, 5 limit rows covering different
    parameter classes.
  - Asserts `stream_permit_limits` returns expected boolean rollup.
  - Asserts `rule_treatable_permit_parameter` fires correctly for a
    facility with 2 treatable parameters → +10.
  - Asserts a facility with 0 treatable parameters returns None.

- `test_attains_linkage.py`
  - Synthetic zip with linkage CSV + impairments CSV.
  - Asserts `stream_attains_linkage` rolls impairments up to NPDES_ID.
  - Asserts `rule_discharges_to_impaired` fires.

- Extend `test_no_events_flag.py`
  - Patch the two new `_download_cached` calls and assert they're NOT
    invoked when `include_events=False`. Same pattern as the existing
    NPDES/SDWA event-load assertions.

- Extend `test_scoring_via_bulk.py`
  - End-to-end: bulk facility row + permit signal → score reflects
    both `rule_significant_violator` AND `rule_treatable_permit_parameter`.

---

## Viewer changes (`chemtreat_water_leads_viewer/index.html`)

Minimal. Per `feedback_viewer_testing`: `node --check` only, no jsdom
harness.

- Two new filter chips in the existing chip bar:
  `Permit covers our chemistry` (filters `tag_treatable_permit=True`)
  and `Discharges to impaired water` (filters
  `tag_discharges_to_impaired=True`).
- Detail panel: add `permitted_parameters_text` and
  `impairment_causes_text` to the compliance-snapshot section
  (alongside the existing `compliance_history_13q` block).
- No banner / schema-version change; the tag chips degrade gracefully
  when older CSVs don't carry the columns (existing `tag_*` rendering
  loop already handles missing keys).

After editing `index.html`, re-run `python -m chemtreat_water_leads_viewer.bake_docs`
to re-embed the methodology tabs.

---

## Risks & open questions

1. **CSV column names are docs-derived, not verified.** First task on
   pickup: download both zips, dump the headers, pin the actual column
   names in the stream readers. MEMORY.md trap #1 applies — EPA's docs
   are 80% right.
2. **Permit-limit cache footprint.** 459 MB pushes the `cache/` dir to
   ~800 MB total. Acceptable on a laptop; flag in `COMMANDS.md` so a
   rep on a metered connection knows.
3. **Parameter substring matching is fragile.** EPA's `PARAMETER_DESC`
   text varies ("BOD, 5-day", "BOD5", "Biochemical Oxygen Demand").
   Mitigation: bias `TREATABLE_PARAM_PATTERNS` toward over-matching;
   verify on a sample run that hit rates are sane (>5% of CWA leads
   should have at least one phosphorus/ammonia/TSS hit nationally).
4. **ATTAINS linkage NPDES_ID format.** EPA sometimes pads permit IDs.
   If the join hit-rate is < 50% on the first run, check for
   leading-zero / whitespace differences between exporter's `NPDES_IDS`
   and the linkage file's `NPDES_ID`. Apply the same normalization
   `kept_npdes_permits` uses (`pid.strip()`).
5. **No diff churn on the new columns.** `snapshot.diff_and_upsert_facilities`
   currently flags rows whose score moved >10. Permit-limit columns
   barely change week-over-week (permits are renewed every ~5 yr), so
   they shouldn't fire `new_facilities` churn. The ATTAINS columns can
   change quarterly as states update 303(d) lists — that's actually
   desirable signal in the diff.
6. **Pre-violation scoring inflation.** Adding +25 max from these two
   rules will shift the distribution. Re-baseline viewer colour tiers
   (`scoreClass` in `index.html`) after the first full nationwide run
   under the new rules — current tiers are ≥110 outlier, ≥80 red, etc.
   (per MEMORY.md). May want to raise the outlier threshold to ≥130.

---

## Out of scope (intentional)

- Per-outfall granularity in the viewer.
- ATTAINS TMDL document links (the `npdes_attains_downloads.zip` may
  carry them; we don't surface them in v1).
- Source-water-protection-area joins for SDWA — different bulk file,
  separate effort.
- API-path parity (see "Pipeline parity" section above).
- Permit-renewal date as a scoring signal — would be a great addition
  but lives in a different file (`npdes_master_general_permits.zip`).

---

## Estimated effort

- Permit limits: 0.5–1 day (stream reader, rule, tests).
- ATTAINS: 0.5 day (smaller file, simpler join).
- Viewer integration: 0.5 day.
- Re-baseline + sanity check on full bulk run: 0.5 day.

**Total: 2–2.5 days end to end**, including verifying column names on
first download and tuning `TREATABLE_PARAM_PATTERNS`.
