"""
sdwa_codes.py
=============
Human-readable lookups for SDWA's numeric reference codes.

EPA's SDWIS database stores everything as codes: violation type ("02"),
contaminant ("5005"), rule family ("210"). Without translation the
output CSVs are unusable by sales. This module is the lookup table.

Coverage:
  We bundle the ~30 most common codes here, drawn from the EPA
  SDWA_REF_CODE_VALUES reference table. This covers ~95% of what shows
  up in violation records for industries and water systems ChemTreat
  cares about. For exhaustive coverage, download the full reference
  CSV from EPA:
      https://echo.epa.gov/tools/data-downloads/sdwa-download-summary
  and extend the dictionaries below, or write a CSV loader.

Why this is a separate module:
  Reference data changes slowly but does change. Isolating it here means
  updates don't touch the API client or pipeline code.
"""

from __future__ import annotations


# ----------------------------------------------------------- violation types
#
# Each violation belongs to one of five regulatory CATEGORIES, which is what
# sales really cares about (it tells them whether the issue is a health
# problem, a paperwork problem, or a treatment-process problem):
#
#   MCL                 - Maximum Contaminant Level exceeded. Health-based.
#                         A real water-quality issue. Highest sales relevance.
#   TreatmentTechnique  - Required treatment process failed (e.g. filtration,
#                         disinfection). Also health-relevant. ChemTreat's
#                         sweet spot - this is what treatment chemistry fixes.
#   Monitoring          - Failed to sample / test on schedule. Procedural, but
#                         often a leading indicator of process problems.
#   Reporting           - Failed to report results to the state. Paperwork.
#   PublicNotification  - Failed to notify customers of a known issue.
#                         Procedural.
#
VIOLATION_CODES: dict[str, tuple[str, str]] = {
    "01": ("MCL",                "Maximum Contaminant Level, single sample"),
    "02": ("MCL",                "Maximum Contaminant Level, average"),
    "03": ("Monitoring",         "Monitoring, regular"),
    "04": ("Monitoring",         "Monitoring, check/repeat/confirmation"),
    "05": ("Reporting",          "State notification"),
    "06": ("PublicNotification", "Public notification"),
    "07": ("TreatmentTechnique", "Treatment technique violation"),
    "08": ("Other",              "Variance/exemption/other"),
    "09": ("Reporting",          "Record keeping"),
    "11": ("Monitoring",         "Initial tap sampling, Pb/Cu"),
    "21": ("Monitoring",         "Follow-up/routine tap sampling, Pb/Cu"),
    "22": ("Monitoring",         "Water quality parameter monitoring"),
    "23": ("Reporting",          "Lead consumer notice"),
    "25": ("TreatmentTechnique", "Lead service line replacement"),
    "26": ("PublicNotification", "Public education, Pb action level"),
    "27": ("Reporting",          "Failure to submit Pb/Cu reports"),
    "29": ("Monitoring",         "Failure to submit Pb/Cu monitoring"),
    "31": ("TreatmentTechnique", "Failure to maintain microbial treatment"),
    "32": ("TreatmentTechnique", "Failure to filter (SWTR)"),
    "36": ("TreatmentTechnique", "Failure to address deficiency"),
    "37": ("TreatmentTechnique", "Failure to maintain disinfection"),
    "41": ("MCL",                "Treatment technique avg / combined filter effluent"),
    "51": ("Monitoring",         "Failure to submit initial monitoring"),
    "71": ("PublicNotification", "Public notification rule (revised)"),
    "75": ("Reporting",          "Routine monitoring reporting"),
    "76": ("Reporting",          "Consumer Confidence Report (CCR)"),
}


# ----------------------------------------------------------- contaminants
#
# Curated subset emphasizing things that show up most often in ChemTreat-
# relevant systems. Coliform / DBP / Lead are the big three for municipal
# treatment opportunities.
#
CONTAMINANT_CODES: dict[str, str] = {
    "1005": "Total Coliform Rule",
    "1040": "E. coli",
    "1074": "Revised Total Coliform Rule",
    "1925": "Disinfection Byproducts",
    "2456": "Atrazine",
    "2950": "Trihalomethanes (TTHM)",
    "2951": "Haloacetic Acids (HAA5)",
    "2980": "Nitrite",
    "2987": "Nitrate",
    "5000": "Lead and Copper Rule",
    "5005": "Lead",
    "5006": "Copper",
    "1040": "E. coli",
}


# ----------------------------------------------------------- rule families
#
# Rule families are the regulatory bucket the violation falls under. Useful
# for grouping leads ("show me all the LCR violators in our territory").
#
RULE_FAMILIES: dict[str, str] = {
    "110": "Total Coliform Rule",
    "111": "Revised Total Coliform Rule",
    "121": "Surface Water Treatment Rule",
    "122": "Interim Enhanced SWTR",
    "123": "Long Term 1 Enhanced SWTR",
    "124": "Long Term 2 Enhanced SWTR",
    "210": "Lead and Copper Rule",
    "220": "Lead and Copper Rule (revised)",
    "310": "Stage 1 D/DBP",
    "320": "Stage 2 D/DBP",
    "330": "Arsenic Rule",
    "410": "Nitrates",
    "420": "Volatile Organic Chemicals",
}


# ----------------------------------------------------------- lookup helpers

def lookup_violation(code) -> tuple[str, str]:
    """Return (category, description). Falls back to ('Unknown', code) for
    codes not in our curated list - which still flows through so sales sees
    the raw code rather than getting silently dropped."""
    key = str(code or "").strip().zfill(2)
    return VIOLATION_CODES.get(key, ("Unknown", f"Unknown code {key}"))


def lookup_contaminant(code) -> str:
    key = str(code or "").strip()
    return CONTAMINANT_CODES.get(key, f"Code {key}" if key else "Unknown")


def lookup_rule(code) -> str:
    key = str(code or "").strip()
    return RULE_FAMILIES.get(key, f"Rule {key}" if key else "Unknown")
