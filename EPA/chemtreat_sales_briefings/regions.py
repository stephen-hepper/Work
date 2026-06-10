"""Region → states → sales lead mapping.

Edit this file freely — it's intentionally a flat Python dict so
adjustments (a state moves between territories, a new regional lead
joins) are a one-line change, not a config-format problem.

The LLM never reads this file directly. The briefings script:
  * exposes region NAMES to the LLM via the `list_regions` tool, and
  * resolves a region NAME to its state list inside every query tool,
so the LLM picks the region by name and the script enforces which
states that name expands to. Region names are the deterministic
boundary; the LLM cannot widen its own scope.

Stub email addresses below — replace with real ones before going live.
The `--dry-run` mode (default) prints to stdout so it's safe to iterate
on these without sending anything.
"""

from __future__ import annotations


# Each region maps to:
#   states:  list of two-letter USPS state/territory codes
#   lead:    {"name": str, "email": str} — sales leader for this territory
REGIONS: dict[str, dict] = {
    "Gulf": {
        "states": ["TX", "LA", "OK", "AR", "MS", "AL"],
        "lead": {"name": "<Gulf Sales Lead>", "email": "gulf-lead@example.com"},
    },
    "Southeast": {
        "states": ["GA", "FL", "SC", "NC", "VA", "WV", "KY", "TN"],
        "lead": {"name": "<Southeast Sales Lead>", "email": "se-lead@example.com"},
    },
    "Northeast": {
        "states": ["PA", "NJ", "NY", "MA", "CT", "RI", "NH", "VT", "ME",
                   "MD", "DE", "DC"],
        "lead": {"name": "<Northeast Sales Lead>", "email": "ne-lead@example.com"},
    },
    "Midwest": {
        "states": ["OH", "IN", "IL", "MI", "WI", "MN", "IA", "MO",
                   "KS", "NE", "ND", "SD"],
        "lead": {"name": "<Midwest Sales Lead>", "email": "mw-lead@example.com"},
    },
    "West": {
        "states": ["WA", "OR", "CA", "AZ", "NV", "UT", "ID", "MT", "WY",
                   "CO", "NM", "AK", "HI"],
        "lead": {"name": "<West Sales Lead>", "email": "west-lead@example.com"},
    },
}


def states_for(region: str) -> list[str]:
    """Resolve a region name to its state list. Raises KeyError on
    unknown names — the script catches this and returns a structured
    error to the LLM so it can pick a valid name on the next turn."""
    return REGIONS[region]["states"]


def lead_for(region: str) -> dict:
    """Sales lead's name + email for a region."""
    return REGIONS[region]["lead"]


def all_region_names() -> list[str]:
    return sorted(REGIONS.keys())
