"""NAICS code → human industry name lookup.

ChemTreat's `TARGET_NAICS` in chemtreat_water_leads/pipeline.py is a
short list of 3-4 digit prefixes; real EPA NAICS columns carry the
full 6-digit codes (sometimes multiple space-separated). This module
translates either form into a sales-readable industry name like
"Fluid Milk Manufacturing" so a rep doesn't have to mentally decode
"311511" before reading a briefing.

Longest-prefix-first lookup, so a 6-digit code like 311511 matches
the more specific "Fluid Milk Manufacturing" entry before falling
through to the broader "311" → "Food Manufacturing".
"""

from __future__ import annotations


# Prefix → human name. Insertion order doesn't matter — `naics_label`
# sorts by length at call time so the most-specific prefix wins.
#
# Two tiers:
#   * Broad 3-4 digit prefixes covering ChemTreat's target industries
#     (matches TARGET_NAICS in chemtreat_water_leads/pipeline.py).
#     These catch any 6-digit code under the prefix.
#   * Finer-grain 6-digit entries for codes that surface frequently in
#     EPA's water-violation data, where the broad label loses useful
#     context (a dairy plant vs. a bakery both classify under 311; the
#     6-digit codes split them).
#
# Add to either tier freely. The lookup picks the longest match.

NAICS_PREFIX_NAMES: dict[str, str] = {
    # Broad ChemTreat target prefixes (TARGET_NAICS in pipeline.py)
    "2111": "Oil & Gas Extraction",
    "212":  "Mining (except Oil & Gas)",
    "2211": "Power Generation, Transmission & Distribution",
    "311":  "Food Manufacturing",
    "312":  "Beverage Manufacturing",
    "322":  "Paper Manufacturing",
    "324":  "Petroleum & Coal Products",
    "325":  "Chemical Manufacturing",
    "327":  "Nonmetallic Mineral Products",
    "331":  "Primary Metal Manufacturing",
    "332":  "Fabricated Metal Products",
    "333":  "Machinery Manufacturing",
    "336":  "Transportation Equipment",
    "622":  "Hospitals",
    # Finer-grain 6-digit entries — high-volume in EPA water data
    "311511": "Fluid Milk Manufacturing",
    "311513": "Cheese Manufacturing",
    "311612": "Meat Processed from Carcasses",
    "311615": "Poultry Processing",
    "311991": "Perishable Prepared Foods",
    "312120": "Breweries",
    "322110": "Pulp Mills",
    "322121": "Paper Mills (except Newsprint)",
    "324110": "Petroleum Refineries",
    "324191": "Petroleum Lubricating Oil & Grease",
    "325110": "Petrochemical Manufacturing",
    "325211": "Plastics Material & Resin",
    "325311": "Nitrogenous Fertilizer Manufacturing",
    "325412": "Pharmaceutical Preparation Manufacturing",
    "325520": "Adhesive Manufacturing",
    "325998": "Specialty Chemical Manufacturing",
    "327310": "Cement Manufacturing",
    "331110": "Iron & Steel Mills",
    "331410": "Nonferrous Metal Smelting & Refining (Copper)",
    "331492": "Secondary Smelting/Refining of Nonferrous Metal",
    "332813": "Electroplating, Anodizing & Coloring of Metal",
    "336111": "Automobile Manufacturing",
    "336390": "Motor Vehicle Parts Manufacturing",
    # SDWA territory — public water systems often carry no NAICS,
    # but the entries below cover the cases where one is present.
    "221310": "Water Supply & Irrigation Systems",
    "221320": "Sewage Treatment Facilities",
}


def naics_label(code: str | None) -> str | None:
    """Translate a NAICS code to a human industry name. Longest-prefix
    match wins. Returns None if no prefix matches or the code is
    empty / malformed.

    The raw `facilities.naics` column sometimes carries multiple codes
    separated by spaces, commas, or pipes (a facility classified
    under several industries). We take the first as the primary —
    EPA's own ECHO UI does the same.
    """
    if not code:
        return None
    # Normalise: first token, stripped of comma/pipe separators.
    first = code.strip().split()[0].split(",")[0].split("|")[0].strip()
    if not first:
        return None
    for prefix in sorted(NAICS_PREFIX_NAMES, key=len, reverse=True):
        if first.startswith(prefix):
            return NAICS_PREFIX_NAMES[prefix]
    return None
