# Basic Commands

Copy-paste reference for running the lead generator. Each section
includes a rough time estimate and notes on what's different between
the first run (cold) and subsequent runs (warm).

## TL;DR — which command should I use?

| Your situation | Use this | First run | Later runs |
|---|---|---|---|
| Just one or two states | `pipeline` (API) | 1–3 min | 30s–2 min |
| Regional sales territory (3–10 states) | `pipeline` (API) | 5–20 min | 2–8 min |
| Most of the lower 48 (20+ states) | `bulk_loader` | 10–20 min | 3–10 min |
| Nationwide | `bulk_loader` | 10–20 min | 3–10 min |
| Just one city / one permit-holder | `pipeline` with one-state filter, then grep | <1 min | <1 min |

**Rule of thumb:** if your territory is more than ~15 states, the bulk
loader is faster. Below that, the API is fine and gets you slightly
fresher data (weekly vs every-7-days cache).

---

## First-time setup

```bash
# Clone or unzip the package, then:
cd chemtreat_water_leads
pip install requests           # only external dependency
```

That's it. No API keys, no database setup, no config files. SQLite
state lives in a single file you pass on the command line.

---

## Single-state pull (API)

The fastest way to get a sense of the tool's output. Good for testing
or for a rep who only covers one state.

```bash
python -m chemtreat_water_leads.pipeline --states TX --out ./out
```

**What happens:** queries CWA across 13 NAICS prefixes (Texas has lots
of refining and chemical mfg, so this is the heavy state), then SDWA
once, then drills into individual violation events for any facility
scoring ≥ 50.

**First run:** **~1–3 min.** All facilities are flagged "new" since the
snapshot DB doesn't exist yet. The `new_facilities_*.csv` will equal
`all_leads.csv`.

**Later runs:** **~30 sec – 2 min.** Same network work, but only the
*deltas* end up in the `new_*.csv` files — usually 0–20 rows. This is
what you actually want sales looking at each day.

---

## Regional pull (API) — typical sales territory

```bash
python -m chemtreat_water_leads.pipeline \
    --states TX,LA,OK,AR,MS,AL \
    --out ./out \
    --db ./snapshot.sqlite
```

Six Gulf-region states is a reasonable example. Adjust to your actual
territory.

**First run:** **~5–15 min.** Most of this is `time.sleep(0.4)` between
API calls being polite to EPA. With 6 states × 13 NAICS = 78 CWA
queries + 6 SDWA queries + drill-down for high-value leads, you're
looking at ~250–400 HTTP calls total.

**Later runs:** **~2–8 min.** Same network volume, but the diff against
the snapshot DB filters output to actual new signals.

---

## Larger multi-region pull (API)

```bash
python -m chemtreat_water_leads.pipeline \
    --states TX,LA,OK,AR,MS,AL,GA,FL,SC,NC,VA,WV,OH,PA,KY,TN \
    --out ./out \
    --db ./snapshot.sqlite \
    -v
```

Sixteen states — most of the Southeast + Appalachia. Verbose mode (`-v`)
helps when you want to see API progress.

**First run:** **~20–45 min.** This is roughly the upper end of what
the API is practical for. If your run is regularly slower than this,
switch to `bulk_loader`.

**Later runs:** **~10–20 min.** Networking dominates regardless.

---

## Nationwide pull (bulk — recommended)

```bash
python -m chemtreat_water_leads.bulk_loader \
    --out ./out \
    --db ./snapshot.sqlite \
    --cache ./cache
```

This downloads three EPA bulk files (cached for 7 days, matching EPA's
weekly refresh cadence), stream-filters them, and produces the same
output shape as the API pipeline.

**First run:** **~10–20 min total.**

- Download `echo_exporter.zip` (~250 MB): 1–5 min depending on connection
- Stream-filter 1.5M facility rows: 1–3 min
- Download `npdes_downloads.zip` (~80 MB): 30s–2 min
- Read NPDES violation events: 1–3 min
- Download `SDWA_latest_downloads.zip` (~40 MB): 15s–1 min
- Read SDWA violation events: <1 min

**Later runs (within 7 days):** **~3–10 min.** Zips are cached and
re-used. Only the parse/filter/diff work runs.

**Later runs (after 7 days):** back to first-run timing because the
cache invalidates.

---

## Nationwide bulk, no events — fastest possible scan

If sales only needs the facility list (not per-pollutant detail), skip
the event drill-downs:

```bash
python -m chemtreat_water_leads.bulk_loader \
    --out ./out --db ./snapshot.sqlite --cache ./cache \
    --no-events
```

**First run:** **~5–10 min** (just `echo_exporter.zip`).
**Later runs:** **~1–3 min.**

---

## Nationwide bulk, filtered to specific states

The bulk loader also accepts `--states`. Useful when you already know
the nationwide cache is fresh and just want a regional cut without
re-running the API loop:

```bash
python -m chemtreat_water_leads.bulk_loader \
    --states CA,WA,OR,AZ,NV \
    --cache ./cache --db ./snapshot.sqlite --out ./out
```

**First run:** ~10–15 min (download + filter).
**Later runs:** **~2–4 min** with warm cache.

---

## Daily cron (nightly diffs to a sales mailbox)

```cron
# At 6 AM every morning, refresh the snapshot and copy new_*.csv
# somewhere humans look. We don't pipe to email here — that's a
# separate step you'd wire up to your SMTP server or Slack webhook.
0 6 * * *  cd /opt/chemtreat && \
           python -m chemtreat_water_leads.pipeline \
             --states TX,LA,OK,AR \
             --out /opt/chemtreat/daily/$(date +\%Y\%m\%d) \
             --db  /opt/chemtreat/state/snapshot.sqlite \
             >> /opt/chemtreat/logs/run.log 2>&1
```

**Each run:** 2–8 min (regional API pull, warm DB).

The cron writes one timestamped directory per run, so the
`new_facilities_YYYYMMDD.csv` files form a daily history you can keep
or rotate. The `snapshot.sqlite` is the source of truth across runs —
**don't delete it** unless you want every facility to look "new" again.

---

## Inspecting the snapshot DB directly

The SQLite file is the audit trail. Useful for ad-hoc questions sales
might ask:

```bash
sqlite3 ./snapshot.sqlite

# How many facilities are we tracking by state?
sqlite> SELECT state, COUNT(*) FROM facilities GROUP BY state ORDER BY 2 DESC;

# Top 20 unresolved leads right now
sqlite> SELECT company, city, state, lead_score
        FROM facilities
        WHERE lead_score >= 60
        ORDER BY lead_score DESC LIMIT 20;

# When was each facility first detected?
sqlite> SELECT company, first_seen FROM facilities
        WHERE state = 'TX' ORDER BY first_seen DESC LIMIT 10;

# Has the lead score for a specific company moved?
sqlite> SELECT company, lead_score, first_seen, last_seen
        FROM facilities WHERE company LIKE '%REFINING%';
```

---

## Resetting / starting over

If you want to wipe history and start fresh (e.g. you changed scoring
rules and want everything re-evaluated):

```bash
rm ./snapshot.sqlite     # next run rebuilds from scratch
```

If you want to keep history but force a re-download of the bulk zips:

```bash
rm -rf ./cache           # forces re-download on next bulk_loader run
```

---

## What output files mean

After any run, the output directory contains:

| File | Purpose | Updates |
|---|---|---|
| `READ_ME_FIRST.txt` | Lag warning. Open first. | Every run |
| `all_leads.csv` | Full ranked inventory of current violators | Every run, overwritten |
| `violation_events.csv` | Underlying individual DMR / SDWA events | Every run, overwritten |
| `new_facilities_YYYYMMDD.csv` | Facilities first seen this run | Per-run dated file |
| `newly_snc_YYYYMMDD.csv` | Facilities that crossed into Significant Non-Complier since last run | Per-run dated file |
| `new_violations_YYYYMMDD.csv` | Individual new violation events since last run | Per-run dated file |

The `new_*` files are what sales actually opens each morning. The
`all_leads.csv` is the standing inventory for territory planning.

---

## Sanity-check times if yours are way off

If a regional run is taking **much** longer than the estimates above:

- **API rate-limiting:** EPA doesn't publish a hard limit, but very
  large runs sometimes slow down. The 0.4s sleep in `pipeline.py` is
  meant to keep us under the threshold; increase it if you see HTTP
  errors in the log.
- **Drill-down volume:** if you have hundreds of high-scoring leads,
  the per-permit `get_effluent_chart` calls dominate. Tune
  `EVENT_DRILLDOWN_MIN_SCORE` in `pipeline.py` upward to limit the count.
- **Network egress:** the bulk loader downloads ~370 MB total on a
  first run. Behind a slow corporate firewall this can be the
  bottleneck.

If a bulk run is failing entirely with a 404, EPA has probably
renamed a file — check the URLs at
<https://echo.epa.gov/tools/data-downloads> and update `BULK_URLS` in
`bulk_loader.py`.
