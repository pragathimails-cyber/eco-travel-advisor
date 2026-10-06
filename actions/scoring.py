"""
scoring.py - pure (side-effect free) functions for carbon classification and
weighted ranking. Kept separate from actions.py so they can be unit tested
without Rasa or network access.

Score formula used to rank options (higher is better):

    S = wc * (1 - C) + wp * (1 - P) + wu * U

    C = min-max normalised carbon (kg CO2e) across the options shown
    P = min-max normalised price (EUR) across the options shown
    U = user-preference match in [0, 1] (preferred mode, travel time)

Weights depend on the user's stated sustainability preference level.
"""
from typing import Dict, List, Optional

# Weights (wc, wp, wu) for each sustainability preference level.
WEIGHTS: Dict[str, Dict[str, float]] = {
    "high":   {"carbon": 0.50, "price": 0.30, "match": 0.20},
    "medium": {"carbon": 0.35, "price": 0.40, "match": 0.25},
    "low":    {"carbon": 0.20, "price": 0.55, "match": 0.25},
}

# Colour thresholds in kg CO2e per passenger-kilometre.
GREEN_MAX = 0.06   # below this: green / "Low"
AMBER_MAX = 0.15   # up to this: amber / "Moderate"; above: red / "High"


def classify_carbon(kg_per_pkm: float) -> Dict[str, str]:
    """Map an emission intensity to a colour band and an accessible text label.

    The text label is always shown alongside the colour so that colour is
    never the only cue (accessibility requirement).
    """
    if kg_per_pkm < GREEN_MAX:
        return {"colour": "green", "label": "Low"}
    if kg_per_pkm <= AMBER_MAX:
        return {"colour": "amber", "label": "Moderate"}
    return {"colour": "red", "label": "High"}


def normalise(values: List[float]) -> List[float]:
    """Min-max normalise a list to [0, 1]. Equal values all map to 0."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def preference_match(option: Dict, preferred_mode: Optional[str],
                     duration_norm: float) -> float:
    """User-preference match U in [0, 1].

    Half the value rewards the user's explicitly preferred transport mode
    (neutral 0.5 if none stated); half rewards shorter travel time.
    """
    if preferred_mode:
        mode_part = 1.0 if option.get("mode") == preferred_mode else 0.0
    else:
        mode_part = 0.5
    return 0.5 * mode_part + 0.5 * (1.0 - duration_norm)


def score_options(options: List[Dict], preference_level: str = "medium",
                  preferred_mode: Optional[str] = None) -> List[Dict]:
    """Attach a score to each option and return them sorted best-first.

    Each option must contain: mode, kg_co2e, price_eur, duration_h.
    """
    if not options:
        return []
    w = WEIGHTS.get(preference_level or "medium", WEIGHTS["medium"])
    c_norm = normalise([o["kg_co2e"] for o in options])
    p_norm = normalise([o["price_eur"] for o in options])
    d_norm = normalise([o["duration_h"] for o in options])

    scored = []
    for o, c, p, d in zip(options, c_norm, p_norm, d_norm):
        u = preference_match(o, preferred_mode, d)
        s = w["carbon"] * (1 - c) + w["price"] * (1 - p) + w["match"] * u
        scored.append({**o, "score": round(s, 3)})
    return sorted(scored, key=lambda o: o["score"], reverse=True)


def score_hotels(hotels: List[Dict], preference_level: str = "medium",
                 budget_per_night: Optional[float] = None) -> List[Dict]:
    """Rank hotels: eco_tier (1-3) stands in for carbon impact, plus price.

    Hotels above the nightly budget get a match value of 0.
    """
    if not hotels:
        return []
    w = WEIGHTS.get(preference_level or "medium", WEIGHTS["medium"])
    p_norm = normalise([h["price_per_night"] for h in hotels])
    out = []
    for h, p in zip(hotels, p_norm):
        eco = (h.get("eco_tier", 1) - 1) / 2.0          # 0..1
        within = budget_per_night is None or h["price_per_night"] <= budget_per_night
        u = 1.0 if within else 0.0
        s = w["carbon"] * eco + w["price"] * (1 - p) + w["match"] * u
        out.append({**h, "score": round(s, 3)})
    return sorted(out, key=lambda h: h["score"], reverse=True)
