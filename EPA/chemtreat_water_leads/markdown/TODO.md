# TODO

Open work on the scoring/output layer, captured from the methodology assessment.
Done items kept here for context; remove once they've shipped a release.

## High-leverage, small change

- [x] **A. Tag columns.** Boolean filters alongside the score so sales can slice
  the inventory in Excel (`tag_active_snc`, `tag_treatment_technique`,
  `tag_mcl_violation`, `tag_lead_copper`, `tag_major_facility`,
  `tag_only_resolved_events`, `tag_chemtreat_high_relevance`). Computed in
  `scoring.compute_tags`, merged into the lead row after drill-down.

- [x] **B. Event-aware scoring.** Rules that read the drilled events, not just
  facility-level summaries. Active-Unaddressed rewarded, all-Resolved demoted,
  Treatment Technique / MCL / Lead-Copper boosted. Removes the 87-point ceiling
  and the "12 Resolved looks like 12 Unaddressed" failure mode.
  `MAX_SCORE` cap removed so true outliers stand out. Verified TX-only run:
  top score 142 (was 87), top-tier ties reduced from 99-at-87 to 3-at-142.

- [x] **C. outreach_posture column.** One-word per facility: `active`,
  `enforcement_underway`, `verify_first`, `historical`, `no_events`. Tells
  sales "should I call?" without parsing reason strings. **Note for viewer
  side:** vocabulary differs from the seed-data values
  (`Unresolved/Addressed/Resolved/Archived`) — viewer needs a small mapping
  dict or first-class support for `historical`/`no_events`. See
  `chemtreat_water_leads_viewer/RATIONALE.md` gap #1.

## Medium-leverage, medium change

- [x] **D. Externalize rule weights.** Every numeric literal (base
  points, multipliers, caps, tier thresholds, demote) lives in a single
  flat `WEIGHTS` dict at the top of `scoring.py`. Rule bodies look up
  into it; sales asks like "bump SNC, drop inspection" are a one-line
  edit. Values unchanged in this refactor, so the 25+ pinned numeric
  assertions across the test suite still hold. YAML loading deferred
  until someone actually needs per-deploy overrides — `WEIGHTS =
  yaml.safe_load(open("weights.yaml"))` is a 2-line swap if so.

- [x] **E. Expose dropped facility metadata.** Added `population_served`,
  `system_type`, `owner_type`, `primary_source` columns to `all_leads.csv`,
  populated from the SDWA API response by `pipeline._flatten_facility`
  (echo_client already requested them via `SDW_WANTED_COLUMNS` — they
  were dropped on the floor). New `rule_population_served` tiers
  +10 / +7 / +4 at ≥50K / ≥10K / ≥3K served. Viewer's
  `renderSdwaContextBlock` surfaces the four fields in the expanded-row
  detail panel for SDWA leads. Bulk SDWA leads leave the cells empty
  (ECHO Exporter doesn't carry PWS metadata at the facility level —
  same asymmetry as `permit_has_*` going the other direction;
  backfilling from the API fine-comb DFR response is a focused
  follow-up).

## Lower-leverage, structural

- [ ] **F. Per-rule strength bands.** Each rule returns
  `(points, reason, strength ∈ {HIGH, MEDIUM, LOW})`. Output a
  `signal_strength_breakdown = "HIGH:2 MEDIUM:1"` column. Tag columns (A)
  cover most of the same need with simpler mechanics; do this if A/B/C aren't
  enough differentiation.

- [ ] **G. Persist score components.** Add per-rule columns (`score_snc`,
  `score_chronic`, `score_formal`, etc., zero when the rule didn't fire).
  Most extensible representation — the total becomes a derived view, and
  sales can pivot/sort on any individual component.

## External data integrations

Tracked separately in `EXTERNAL_DATA_STATUS.md`. As of 2026-06:

- [x] **Tier-1 #1: NPDES Permit Limits** — pre-violation signal, shipped.
- [x] **Tier-1 #2: ATTAINS-NPDES catchment** — pre-violation signal, shipped.
- [x] **Tier-1 #3: NPDES DMR archive** — active-compliance signal, shipped.
- [x] **Tier-1 #4: Sewer Overflow / CSO / SSO events** — daily refresh,
  POTW lead signal. Shipped 2026-06-16 (events feed + collection-system
  enrollment + National CSO Inventory + POTW NAICS widening). Plan in
  `CSO_SSO_PLAN.md`; live hit rates in `EXTERNAL_DATA_STATUS.md`.
- [ ] **Tier-1 #5: TRI Surface Water Releases** — annual chemical-specific
  pounds-per-year. Not started.
- [ ] **Tier-2 #6: UCMR5 PFAS Occurrence** — needs sales confirmation that
  ChemTreat sells PFAS treatment.
- [ ] **Tier-2 #7: Industrial Stormwater MSGP AIM events** — niche but
  high-confidence.

## Other open follow-ups

- [ ] **Retry-on-empty for state-wide queries.** LA/OH CWA queries sometimes
  return empty without a QID under rapid-fire pacing. The DFR-retry pattern
  in `fetch_sdwa_violation_events` could be lifted to `_qid_workflow`.

- [x] **Tune `EVENT_DRILLDOWN_MIN_SCORE` (resolved 2026-06-02).** The
  pre-violation + active-compliance integrations lifted top CWA scores
  from 47 → 187. ~10K leads now clear the ≥50 threshold nationwide. The
  threshold is correctly tuned; no change needed. Closed.

- [ ] **`sdwa_codes.py` may be redundant** for the DFR drill-down path now
  that violations come with text fields (`FederalRule`, `ContaminantName`,
  `ViolationCategoryDesc`). Audit usage; keep only what the bulk loader
  still needs.

- [ ] **Email digest of `new_today.csv`.** Sales ops asked. ~30 lines via
  SMTP. Skipped while we were chasing data-accuracy bugs.

- [ ] **Re-tier viewer color thresholds.** The pre-2026-06 outlier band was
  ≥110. The 2026-06-02 run had 220 leads ≥130, 998 ≥100, and a top of 187.
  The 2026-06-16 post-CSO/SSO run had 2,996 leads ≥100, 306 ≥150, 0 ≥200
  with top still at 187 — POTW NAICS widening expanded the inventory by
  ~8.5K but didn't push the top score past the prior ceiling. Consider
  bumping the outlier threshold to ≥150 so the star badge stays rare and
  meaningful. (Score slider max already bumped 180 → 220 in `index.html`
  on the cso-sso-integration branch as a holding pattern; the `scoreClass`
  thresholds still need the actual re-baseline.)

- [x] **Mirror `run_health.json` into the DB so `dump_run` can materialize
  it (shipped 2026-06-16).** `runs.run_health_json TEXT` column added;
  `bulk_loader.run_bulk` and `pipeline.run` now persist the JSON
  alongside the on-disk file; `dump_run` writes it into
  `materialized/run_<N>/run_health.json`. Single viewer-uploadable
  folder; legacy pre-2026-06-16 runs skip cleanly with a log line
  pointing back to `out/<run-folder>/`.

- [x] **WET / limit-of-zero DMR sentinel display (shipped 2026-06-16).**
  EPA's `EXCEEDENCE_PCT` is INT32_MAX on rows where the permit limit is
  zero (chlorine residual at no-detect permits) or where the parameter
  is a pass/fail biological assay (Whole Effluent Toxicity tests). The
  bulk_loader display-clamps to 99,999% for storage; `rule_recent_dmr_
  exceedance` now special-cases pct ≥ 99,999 and emits a parameter-aware
  "pass/fail or limit-of-zero parameter failure" reason instead of the
  misleading "99999% over limit". Tier weight unchanged at +15.
