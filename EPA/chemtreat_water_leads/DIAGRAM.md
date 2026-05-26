# Data state map

How the two entry points (`bulk_loader` and `pipeline`) populate the
SQLite source of truth, and how to move between depths of detail.

---

## States you can land in

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Empty
    Empty           --> Facilities_only : bulk_loader --no-events
    Empty           --> Full            : bulk_loader
    Facilities_only --> Full            : bulk_loader
    Full            --> Mixed           : pipeline --states X,Y
```

| State            | Coverage   | Per-event detail                  | SDWA breadth         |
|---               |---         |---                                |---                   |
| Facilities only  | nationwide | _none_                            | SNC + formal (tight) |
| Full             | nationwide | full DMR on top leads, codes only on the rest | SNC + formal (tight) |
| Mixed            | both       | full DMR everywhere in chosen states | broader in those states (`p_viola=Y`) |

Every transition writes through `snapshot.sqlite`; the DB never
deletes rows, so paths layer additively.

---

## What `bulk_loader` does internally — one command, five stages

```mermaid
flowchart TD
    A[1. Scan ECHO Exporter<br/>→ facility inventory] --> B[2. Stream NPDES + SDWA event zips<br/>→ join to leads]
    B --> C[3. Re-score with events<br/>compute outreach_posture + tags]
    C --> D[4. API fine-comb<br/>on high-value / newly-discovered / score-jumped leads]
    D --> E[5. Upsert SQLite<br/>→ dump CSVs]
```

`bulk_loader` (no flags) runs **all five stages in one process**, ~15-30 min
total. You don't need to issue each stage separately. `--no-events` stops
after stage 1.

---

## What's in `out/` after each command

```
out/
├── READ_ME_FIRST.txt          ← lag warning, always
├── all_leads.csv              ← every lead this run touched, ranked
├── violation_events.csv       ← every event tied to those leads
├── run_health.json            ← run metadata + warnings + signals
├── new_facilities_YYYYMMDD.csv    ← facilities first seen in THIS run
├── newly_snc_YYYYMMDD.csv         ← facilities that just crossed SNC
└── new_violations_YYYYMMDD.csv    ← events first seen in THIS run
```

The first four files are overwritten every run (current state). The
`new_*` files are dated — one per run — so a daily/weekly history
accumulates.

**Upload to the viewer**: `all_leads.csv`, `violation_events.csv`,
and `run_health.json` together. The first two populate the
Inventory tab; the JSON populates the Run Health tab with coverage
gaps, depth gaps, run warnings, and suggested follow-up commands.

On a first run from an empty DB, the three `new_*` files are
essentially copies of `all_leads.csv` / `violation_events.csv` (no
baseline to diff against). On a later run, they hold only the
genuinely fresh rows since the previous run.

---

## Cost of each path

|                           | Time             | EPA load                              |
|---                        |---               |---                                    |
| `bulk_loader --no-events` | 5–10 min         | 1 download (~250 MB), zero API calls  |
| `bulk_loader`             | 15–30 min        | 3 downloads (~830 MB) + auto fine-comb |
| `pipeline --states X`     | 5–20 min × state | hundreds of API calls per state       |

After 7 days, `bulk_loader` re-downloads (cache invalidates to match
EPA's weekly refresh). Inside that window, runs are ~3-10 min because
the zips are cached.

---

## "Which state am I in?"

```bash
sqlite3 snapshot.sqlite "
  SELECT program, outreach_posture, COUNT(*)
  FROM facilities GROUP BY 1, 2 ORDER BY 1, 2"
```

- All `no_events` → **Facilities only**
- Mix of `active` / `enforcement_underway` / `verify_first` → **Full**
- One or two states with mostly `active` / rich SDWA detail → **Mixed**
