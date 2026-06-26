# ChemTreat Water Violation Lead Generator — Executive Summary

**What it does**
- Turns public EPA water-compliance data into a weekly ranked list of facilities most likely to need water-treatment chemistry, surfacing roughly fifty highest-conviction leads out of an inventory approaching sixty-five thousand facilities.

**Data sources pulled and aggregated**
- **ECHO facility inventory** — every U.S. permitted water facility with current-quarter compliance flags, Significant Non-Complier (SNC) designations, formal enforcement actions, and penalty history.
- **Clean Water Act DMR archive** — discharge-monitoring reports showing which pollutants facilities are currently exceeding and by how much.
- **NPDES permit limits** — catalog of what each facility's permit allows them to discharge (a pre-violation signal: the account is *set up* to need chemistry whether or not they're failing today).
- **ATTAINS 303(d) impaired-waters linkage** — flags facilities discharging into already-impaired waterbodies where state regulators are obligated to tighten limits at the next permit renewal.
- **Safe Drinking Water Act violation feed** — public water systems with treatment-technique, maximum-contaminant-level, and lead-and-copper violations.
- **eRule Phase 2 sewer-overflow / bypass events** — the only daily-cadence signal in EPA's portfolio. Flags publicly owned treatment works the day after a sanitary sewer overflow, raw-sewage event, or treatment-plant bypass.
- **National Combined Sewer Overflow Inventory** — supplemental list of POTWs with combined storm-and-sanitary sewer systems that overflow more during wet weather.

**How leads are scored and ranked**
- Every facility gets a single score that is the sum of contributions from named rules — fully transparent, fully auditable, with a human-readable reason per rule (e.g. "Significant Non-Complier: 40 points" or "Discharges parameter matching impaired-waterbody cause: 15 points").
- Strongest signals: SNC designation, chronic quarterly non-compliance, currently-exceeding-a-permitted-treatable-parameter, dry-weather sanitary sewer overflows, treatment-technique violations.
- A "do not call" guardrail demotes leads whose violations are already resolved so sales doesn't ambulance-chase fixed issues.

**How the output is consumed**
- A single ranked CSV plus a browser-based viewer that lets sales filter by signal type — chemistry-relevant exceedances, recent sewer overflows in dry weather, combined sewer systems, discharges to impaired water, etc. — and drill into any facility's score reasoning, violation history, and EPA-direct profile link.

**LLM-drafted regional sales briefings**
- A weekly automated process queries the ranked inventory and uses an Azure OpenAI model to draft a short, regional briefing for each sales territory's leader — written in natural language, framed for outreach, naming specific facilities and explaining why each is worth the rep's time this week.
- The model can only ask narrowly-defined questions of the data (e.g. "what are this region's never-before-briefed leads," "what new sewer overflows happened in Texas this week") and can't generate free-form database queries — every fact in the briefing traces back to the score breakdown.
- Briefings can be auto-emailed to the right sales lead by region, with a "dry-run" mode for tone iteration and a tracking layer so the same lead doesn't get re-briefed until something material about it changes.

**Caveat baked in everywhere**
- All EPA feeds carry a 30–90 day reporting lag *except* the new sewer-overflow feed (now ~1 day). Every output — viewer banner, CSV row, briefing email — surfaces the lag note and instructs reps to verify a facility's current status before any outreach.
