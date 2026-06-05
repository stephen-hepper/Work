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

- [ ] **D. Externalize rule weights.** Replace inline literals (`return 40, ...`)
  with a `WEIGHTS` dict at the top of `scoring.py`, or load from
  `weights.yaml`. Sales feedback like "weight SNC less, TT more" becomes a
  config change instead of a code review. **Urgency raised 2026-06:** with
  10 facility rules + 5 event rules now in play (up from 6+5), the
  config-vs-code-review delta is bigger. Worth doing before the next
  weight-tuning conversation with sales.

- [ ] **E. Expose dropped facility metadata.** Add `population_served`,
  `system_type`, `owner_type`, `primary_source` columns from the SDWA response
  to `all_leads.csv`. Add a `rule_population_served` that rewards systems
  serving 3K+/10K+/50K+ people (revenue proxy for SDWA).

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
- [ ] **Tier-1 #4: Sewer Overflow / CSO / SSO events** — daily refresh,
  POTW lead signal. Not started.
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
  Consider bumping the outlier threshold to ≥150 so the star badge stays
  rare and meaningful.
