"""Shared variant-choice matching for multi-option supplier listings.

A variant listing offers combinations like Colour x Size. An employee picks
one by typing the option text into the Sheet's "Variant Choice" column
(e.g. "army green xl", in any order, any capitalisation). A combination
matches when every one of its option values appears as a whole word/phrase
in the typed text; among matches, the one covering the most typed
characters wins - so "army green xl" picks Army Green/XL, not Green/XL.
No unique winner = ambiguous; the caller flags the row with the options.
"""
import re


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def match_variant_choice(choice, candidates):
    """candidates: [(key, [value, ...]), ...] - one entry per combination.
    Returns (key, None) on a unique best match, otherwise (None, "no_match")
    or (None, "ambiguous")."""
    padded_choice = f" {_norm(choice)} "
    scored = []
    for key, values in candidates:
        total = 0
        for value in values:
            nv = _norm(value)
            if not nv or f" {nv} " not in padded_choice:
                total = -1
                break
            total += len(nv)
        if total > 0:
            scored.append((total, key))
    if not scored:
        return None, "no_match"
    scored.sort(key=lambda t: -t[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "ambiguous"
    return scored[0][1], None


def options_text(candidates, limit=12):
    """Human-readable list of the available combinations, for the Change
    Alert cell. Deduped, capped so the cell stays readable."""
    combos = []
    seen = set()
    for _, values in candidates:
        combo = " / ".join(str(v).strip() for v in values if str(v).strip())
        if combo and combo not in seen:
            seen.add(combo)
            combos.append(combo)
    shown = combos[:limit]
    text = " | ".join(shown)
    if len(combos) > limit:
        text += f" | ... and {len(combos) - limit} more"
    return text
