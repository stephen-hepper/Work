# Starting Guide

For a sales rep using the lead generator for the first time. Assumes
someone has set up Python and the project's dependencies; if you can
type the commands below into a Terminal window, you're set.

---

## What this tool does

It pulls EPA water-violation data, scores facilities for ChemTreat
relevance, and produces a ranked inventory of leads you can browse in
a viewer. Every score comes with a human-readable reason
("Significant Non-Complier, 4 quarters in non-compliance, $48k recent
penalty") — there is no ML black box.

---

## Acronyms you'll see

**EPA programs and data terms:**

| Acronym | Stands for | What it means here |
|---|---|---|
| **EPA** | Environmental Protection Agency | The federal agency that publishes the data we pull. |
| **CWA** | Clean Water Act | The federal law covering industrial **wastewater** discharges. CWA leads = industrial facilities (refineries, paper mills, power plants, food & beverage, etc.). |
| **SDWA** | Safe Drinking Water Act | The federal law covering **drinking-water systems**. SDWA leads = public water utilities, municipal systems, schools, mobile-home parks with their own well, etc. |
| **NPDES** | National Pollutant Discharge Elimination System | The permit program under CWA. An NPDES permit number identifies a specific industrial discharge point. |
| **DMR** | Discharge Monitoring Report | The monthly report a CWA facility files showing what they measured in their discharge. DMR data is where exceedances (over-the-limit readings) show up. |
| **SNC** | Significant Non-Complier | EPA's red-flag designation for facilities with serious, recurring violations. The strongest single signal in our scoring. |
| **MCL** | Maximum Contaminant Level | A health-based ceiling on a specific contaminant in drinking water (lead, nitrate, etc.). MCL violations are high-relevance SDWA signals. |
| **TT** | Treatment Technique | A required treatment process for a drinking-water system (filtration, disinfection). TT violations mean the treatment is failing — the single highest-relevance category for ChemTreat. |
| **ECHO** | Enforcement and Compliance History Online | EPA's public website at `echo.epa.gov`. Every facility row in the viewer has a link to its ECHO page for verification. |
| **DFR** | Detailed Facility Report | A composite per-facility page on ECHO combining CWA + SDWA + FRS data. The viewer's "Open in EPA ECHO" link goes here. We also drill it programmatically for SDWA per-violation detail. |
| **FRS** | Facility Registry Service | EPA's master facility ID system. `RegistryID` comes from here and links a single physical site across CWA and SDWA records. |
| **ICIS-NPDES** | Integrated Compliance Information System / NPDES | EPA's internal database backing the CWA data. Backstage — the API hits it for you. |
| **SDWIS-Fed** | Safe Drinking Water Information System / Federal | EPA's internal database backing SDWA data. Same story — backstage. |
| **PWS** | Public Water System | The SDWA-regulated entity. Can be a city utility, a rural water authority, a school, or a mobile-home park with its own well. One PWS often serves multiple cities/counties. |
| **TMDL** | Total Maximum Daily Load | The pollutant cap a state must write for an impaired waterbody. When a TMDL gets written, downstream NPDES permits typically tighten at next renewal — that's the "pre-violation" angle on `discharges_to_impaired`. |
| **303(d)** | Section 303(d) of the Clean Water Act | Requires states to list impaired waterbodies. Our `discharges_to_impaired` flag fires when a facility's outfall sits upstream of one. |
| **ATTAINS** | Assessment, TMDL Tracking, and Implementation System | EPA's database of state water-quality assessments and 303(d) lists. Source of the impaired-water linkage. |
| **NAICS** | North American Industry Classification System | Industry codes. We filter on prefixes (`325` chemical mfg, `2211` power gen, etc.) so the inventory stays focused on ChemTreat-target industries. |
| **SIC** | Standard Industrial Classification | NAICS predecessor; some EPA records still carry SIC codes alongside NAICS. |
| **FY** | Fiscal Year | Federal FY runs Oct 1 – Sep 30. The DMR archive file is named by federal FY (`npdes_dmrs_fy2026.zip` covers Oct 2025–Sep 2026). |

**Computer/technical terms:**

| Acronym | Stands for | What it means here |
|---|---|---|
| **API** | Application Programming Interface | A way for our code to query EPA's live data directly. Slower than bulk downloads but produces richer per-event detail. |
| **REST** | Representational State Transfer | The pattern EPA's API uses — HTTP requests that return JSON. The `echo_client.py` module is the REST client. |
| **CSV** | Comma-Separated Values | A text file you can open in Excel. The viewer reads these to display the inventory. |
| **JSON** | JavaScript Object Notation | A structured text file format. `run_health.json` is the only one you'll see. |
| **DB** | Database | The `snapshot.sqlite` file that stores the running history of every facility we've ever seen. Don't delete it. |

**Datasets queued for future integration** (you may see these in the Run Health tab or in `EXTERNAL_DATA_STATUS.md`):

| Acronym | Stands for | What it means here |
|---|---|---|
| **CSO** | Combined Sewer Overflow | A sewer system that combines stormwater and sanitary; overflow events feed the `tag_recent_sewer_overflow` / `tag_combined_sewer_system` chips. Shipped 2026-06-16 (Tier-1 #4) on a daily refresh cadence — the only sub-30d lag signal in the project. |
| **SSO** | Sanitary Sewer Overflow | Same family as CSO, sanitary-only. Tagged separately as `tag_recent_sso` because it almost always indicates treatment-process failure (raw sewage where it shouldn't be). |
| **TRI** | Toxics Release Inventory | EPA's annual per-facility per-chemical pounds-released report. Queued (Tier-1 #5) for the chemical-specific surface-water release dimension. |
| **UCMR5** | Unregulated Contaminant Monitoring Rule, round 5 | The current round of EPA's mandate that PWSes test for emerging contaminants. UCMR5 is the PFAS-monitoring round. Queued (Tier-2 #6) pending sales confirmation that ChemTreat sells PFAS chemistry. |
| **PFAS** | Per- and Polyfluoroalkyl Substances | "Forever chemicals." UCMR5's headline target. |
| **MSGP** | Multi-Sector General Permit | EPA's umbrella NPDES permit for industrial stormwater. Facilities under MSGP that trip thresholds get pushed into the AIM tier. |
| **AIM** | Additional Implementation Measures | The MSGP escalation tier. A facility in AIM is forced into specific treatment actions — queued (Tier-2 #7) as a high-confidence signal. |
| **WQX** | Water Quality Exchange | USGS/EPA ambient water-quality measurements (~430M records). Deferred — needs HUC-based spatial joining to be useful. |
| **HUC** | Hydrologic Unit Code | USGS watershed identifiers used to spatially link facilities to water bodies. Needed for the deferred WQX integration. |
| **USGS** | U.S. Geological Survey | Publishes WQX ambient data. |

For the full data-source catalog (refresh cadence, reporting lag, what each source produces),
see [`DATA_DESCRIPTION.md`](DATA_DESCRIPTION.md).

Two ways to pull data:

- **`bulk_loader`** — covers all 50 states + DC + territories
  nationwide via EPA's weekly bulk CSV downloads. ~15-30 min total
  per run. This is what you'll run most weeks.
- **`pipeline --states X,Y,Z`** — calls the live EPA API per state.
  Slower per state but produces richer per-event detail (which
  pollutant, what the limit was, by how much they exceeded it). Use
  this as a follow-up on specific states where bulk's depth isn't
  enough.

---

## First run, from scratch

From a Terminal window inside the project's `EPA/` folder:

```bash
../.venv/bin/python -m chemtreat_water_leads.bulk_loader \
    --out ./out \
    --db ./snapshot.sqlite \
    --cache ./cache
```

That's the whole command. **No `--states` flag means nationwide** —
every state, DC, and territory. The first time you run it, EPA's
weekly bulk files download (about 2.2 GB total across six files,
cached locally for 7 days afterward).

The run takes ~10-30 minutes (faster on subsequent runs within the
7-day cache window). Inside that one process it:

1. Scans EPA's nationwide facility list (~1.5M rows) and keeps the
   ones with water-violation signals.
2. Adds **pre-violation signals** — which ChemTreat-treatable
   parameters each permit covers (phosphorus, ammonia, TSS, BOD,
   oil/grease, metals incl. iron/manganese, cyanide, chlorine
   residual, microbiological — coliform / E. coli / Enterococci /
   fecal indicators), and whether each facility discharges to a
   downstream 303(d)-impaired waterbody.
3. Adds **active-compliance signals** — which facilities are
   currently exceeding their permit limits this fiscal year, and on
   which chemicals.
4. Joins per-event details from EPA's bulk NPDES and SDWA event files.
5. For high-scoring and newly-discovered leads, calls EPA's live API
   to pull richer SDWA per-event detail bulk doesn't carry. (If
   EPA's API is rate-limiting our IP, this step bails out gracefully
   after a 20-call streak and the affected leads show up in Run
   Health for a later re-run.)
6. Saves everything to `snapshot.sqlite` and dumps a clean set of CSVs
   into `out/`.

You don't invoke any of those stages separately — one command does
the whole chain.

---

## What lands in `out/` after the run

Each run gets its own subfolder (e.g.
`out/bulk_nationwide_20260612-090000/`) so runs never overwrite each
other. Inside the folder there are only TWO files:

```
out/<run-folder>/
├── run_health.json                ← upload to viewer
└── newly_snc_YYYYMMDD.csv         dated, "newly Significant Non-Complier"
                                   (skipped if the diff is empty — common on first runs)
```

Everything else (`all_leads.csv`, `violation_events.csv`, the daily
diffs) lives in `snapshot.sqlite` and is built on demand. The
end-of-run log prints the exact command to materialize them; it looks
like:

```bash
python -m chemtreat_water_leads.dump_run \
    --db ./snapshot.sqlite --latest \
    --out ./materialized/run_latest
```

That writes `all_leads.csv`, `violation_events.csv`,
`new_facilities.csv`, and `new_violations.csv` into the target folder.
The `new_*` files are mostly empty on a first run since there's no
prior baseline to diff against — they become useful on weekly re-runs.

**Why the split.** `snapshot.sqlite` is the source of truth; the big
CSVs are pure views of it. Materializing on demand keeps the run
folders tiny (~100 KB) and the DB the single canonical state.

---

## A note on how drill-down picks leads

You don't have to think about this on a first run, but it helps to
know why some leads end up with rich event detail and others don't.

Inside the bulk run, every facility's events go through two passes:

1. **Bulk drill-down (free, applies to all leads).** The bulk NPDES
   and SDWA event files are streamed and joined to every kept
   facility. If the bulk feed had events for that facility, they're
   attached. This is automatic and adds no time per lead.

2. **API fine-comb (selective).** After the bulk pass, the live EPA
   API is called for the leads that matter most:
   - Lead score ≥ 50 — always drilled
   - Newly-discovered AND score ≥ 20 — drilled (this catches every
     lead on a first-from-scratch run)
   - Score jumped > 10 since the prior run AND score ≥ 20 — drilled
   - Already has events from the bulk pass — **skipped**
   - Score below 20 — **skipped** (too low to act on regardless)

So nothing gets "flagged for later." The deep drill runs *during* the
bulk run on the leads that warrant it. If a lead ends up with no
event detail after the run, it's because (a) the bulk feed had
nothing AND (b) either the API attempt also failed (bot-block, EPA
throttle) or the lead scored too low to qualify.

The Run Health tab in the viewer surfaces case (a) plus failed-API
cases so you know which states might be worth a follow-up `pipeline`
run.

---

## Opening the viewer

First materialize the CSVs the viewer eats — the bulk/pipeline runs
only write `run_health.json` inline:

```bash
python -m chemtreat_water_leads.dump_run \
    --db ./snapshot.sqlite --latest \
    --out ./materialized/run_latest
```

Then open `chemtreat_water_leads_viewer/index.html` in any browser
(Chrome, Safari, Firefox — no internet required).

1. Click **Upload files** at the top right.
2. Select **all three** files at once, all from `./materialized/run_latest/`:
   - `all_leads.csv`
   - `violation_events.csv`
   - `run_health.json` (mirrored out of `runs.run_health_json` by
     `dump_run` since 2026-06-16 — same folder as the CSVs. Runs from
     before that date won't have it in the materialized folder; for
     those, grab the JSON from the original `out/<run-folder>/`.)
   On Mac, cmd-click each to select multiple in the file picker.
3. The viewer auto-detects each file by its columns/schema. Pick them
   in any order.

You'll land on the Inventory tab by default. The Run Health tab is to
its right.

---

## What to look at first, every run

### 1. Run Health tab

This is where you find out whether the run's data is trustworthy and
where the gaps are.

- The **red badge** on the tab is a count of "things worth attention"
  this run. If it's high, click in.
- The **Coverage gap** card flags high-scoring leads with no event
  detail, broken down by state. If it says something like "12
  high-score leads in TX/LA/PA have no event detail," that's the cue
  to run a follow-up API pull on those states. The card gives you a
  copy-pasteable command.
- The **Depth gap** card flags CWA leads where bulk gave us only
  violation codes, not specific pollutant readings. Same fix —
  `pipeline --states ...`.
- The **Run warnings** panel lists any EPA bot-block or throttle
  errors. If it's empty, the run was clean.

### 2. Inventory tab

Standard browsing:

- Default sort is by score, highest first.
- Filter to your territory in the **State** dropdown.
- Click any row to expand. You'll see the full score reasoning, the
  underlying violation events, and a direct link to EPA's facility
  detail page.
- **Resolved (green-tinted)** rows are "they fixed it — do not
  cold-call." The Status filter hides these by default.
- A **Newly SNC** badge means the facility just crossed into
  Significant Non-Complier status since the last run. These are the
  freshest signal sales has.

**Two extra chip groups** for slicing by signal class:

- **Pre-violation signals.** Use these to find leads *before* they've
  failed. The chips:
  - *Permit covers our chemistry* — facility's NPDES permit allows
    discharge of a ChemTreat-treatable parameter. They're a buyer
    even at 100% compliance.
  - *Discharges to impaired water* — outfall is upstream of a 303(d)
    waterbody. The state will tighten limits at next permit renewal.
  - *Effluent matches impairment cause* — the strongest pre-violation
    signal: state has documented that THIS facility's monitored
    parameter causes the downstream impairment.

- **Active compliance signals.** Use these to find leads currently
  exceeding their limits:
  - *Currently exceeding a permit limit* — any DMR exceedance in the
    loaded fiscal year.
  - *Exceeding our chemistry parameter* — **strongest single signal
    in the system**: permit covers the parameter AND they're
    currently exceeding it. Sales call writes itself.

Both chip groups are AND-semantics: check multiple chips to narrow.
The expanded-row view shows the underlying detail (which permitted
parameters, which exceedances, how badly).

---

## Want richer detail on a specific territory?

If the Run Health tab flags a gap, follow the suggested command.
Example:

```bash
../.venv/bin/python -m chemtreat_water_leads.pipeline \
    --states TX,LA,PA \
    --out ./out \
    --db ./snapshot.sqlite
```

That takes 5-20 minutes per state and adds:

- Broader SDWA inventory (every system with any open violation, not
  just SNC/formal-action ones — roughly 10× more systems per state).
- Full per-DMR detail on CWA events (which pollutant, the permitted
  limit, the measured value, the exceedance percent).

It updates the same DB and CSVs. Re-upload them to the viewer to see
the deeper detail.

---

## Weekly refresh routine

Seven days later, run the same `bulk_loader` command — EPA refreshes
the bulk files weekly, so the cache invalidates automatically:

```bash
../.venv/bin/python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache
```

This time the `new_*` files inside the materialized folder are the
interesting ones — they hold only what changed since the prior run. Re-run
`dump_run --latest --out ./materialized/run_latest` to refresh the
materialized CSVs against the new run. Since 2026-06-16 `dump_run` also
materializes `run_health.json` into the same folder (mirrored from
`runs.run_health_json` in the DB), so you upload three files all from
`materialized/run_latest/` — no longer need to grab the JSON from
`out/<run-folder>/` separately.

**Critical rule:** never delete `snapshot.sqlite`. It's the diff
baseline. If you delete it, the next run treats every facility as
"new again" and the delta files become useless.

---

## Three rules of thumb

1. **EPA data lags 30-90 days.** SDWA is ~90 days; CWA is ~30-45.
   Verify status on the EPA ECHO page before any outreach. The
   "Resolved — do not call" filter exists for this reason.
2. **Score is heuristic.** 78 vs 82 is noise; 78 vs 40 is signal.
   The color tiers in the viewer reflect this.
3. **Read the Run Health tab first, Inventory second.** Health tells
   you whether the data is trustworthy and where the gaps are.
   Inventory is where you act on it.
