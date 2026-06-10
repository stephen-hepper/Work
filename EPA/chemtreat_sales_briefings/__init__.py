"""Regional sales briefings driven by an LLM with narrow DB-query tools.

Separate from chemtreat_water_leads/: that package owns data ingest +
scoring; this package owns regional outreach drafting. The contract
between them is `snapshot.sqlite` — this package only reads it.
"""

__version__ = "0.1.0"
