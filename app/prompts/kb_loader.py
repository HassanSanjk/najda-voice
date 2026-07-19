"""
Knowledge base loader and scenario matching.

KB files live in /knowledge as YAML, matching the schema in your
KB_Bleeding.yaml: emergency, languages, triage, scenarios (branch_key ->
per-language steps/escalate/follow_up), general_knowledge (per-language
q/a list).

SCENARIO MATCHING: checks each KB file's own `keywords: {en: [...], ar:
[...]}` field first if present. Falls back to KEYWORDS_FALLBACK below
(keyed by the file's `emergency` field) otherwise -- meaning matching
works today without requiring every file to have a keywords field yet.
If matching misses on real call transcripts during testing, adding an
explicit `keywords` field per file is the cleaner long-term fix.

LIMITATION: matching is plain substring search, not tokenized or
normalized. This is more fragile for Arabic than English (diacritics,
hamza variants, and spelling variation can all cause misses) -- worth
specifically testing with real Arabic phrasing, not just the English
side, before trusting this in a demo.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"

# Used only for KB files that don't define their own `keywords` field.
# Not exhaustive -- expand based on what real callers actually say.
# Single-word keywords, not phrases — avoids word-order sensitivity and
# contraction misses ("can't" vs "cannot", "broken arm" vs "arm is broken").
# Normalization (apostrophe removal) happens before matching so both
# forms hit the same entries.
KEYWORDS_FALLBACK = {
    "bleeding": {
        "en": ["bleed", "bleeding", "blood", "cut", "wound", "gash"],
        "ar": ["نزيف", "دم", "جرح", "قطع"],
    },
    "burns": {
        "en": ["burn", "burned", "burnt", "scald", "fire"],
        "ar": ["حرق", "حروق", "احتراق"],
    },
    # NOTE: "breathing"/"تنفس" deliberately NOT keywords for choking or cpr —
    # breathing is mentioned in nearly every emergency ("he's breathing",
    # "not breathing normally"), so it routed unrelated emergencies (observed
    # live: a car-crash victim got choking/Heimlich guidance) to whichever of
    # the two files happened to sort first — which even differs by OS.
    "choking": {
        # "cannot breathe" is a phrase, but it's contraction-stable: the
        # normalizer maps "can't breathe" to the same string before matching.
        "en": ["choke", "choking", "airway", "throat", "heimlich", "cannot breathe"],
        "ar": ["اختناق", "شرقة", "يختنق", "غصة"],
    },
    "cpr": {
        "en": ["pulse", "unconscious", "cpr", "heart", "collapsed"],
        # "مغمى/أغمي عليه" = colloquial "passed out/unconscious" — what real
        # callers actually say (observed live, repeatedly).
        "ar": ["نبض", "فاقد الوعي", "قلب", "إنعاش", "مغمى", "أغمي"],
    },
    "electric_shock": {
        "en": ["electric", "shock", "electrocuted", "power"],
        "ar": ["كهرباء", "صعقة", "صعق"],
    },
    "fractures": {
        "en": ["broken", "fracture", "break", "snapped", "bone"],
        "ar": ["كسر", "عظم"],
    },
    "snake_bites": {
        "en": ["snake", "bite", "bit", "bitten"],
        "ar": ["ثعبان", "لدغة", "عضة"],
    },
    "allergic_reactions": {
        "en": ["allergic", "allergy", "anaphylaxis", "hives", "epipen"],
        "ar": ["حساسية", "تحسس", "تورم"],
    },
}


@lru_cache(maxsize=None)
def _load_yaml(filename: str) -> dict:
    path = KNOWLEDGE_DIR / filename
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def _all_kb_files() -> list[Path]:
    # Explicit case-insensitive sort: first-match-wins ordering must be
    # identical everywhere. Windows Path comparison is case-insensitive and
    # Linux's is not, which made KB_Choking vs KB_CPR match in a different
    # order per OS (confirmed live vs. test-box divergence).
    return sorted(KNOWLEDGE_DIR.glob("KB_*.yaml"), key=lambda p: p.name.lower())


# Normalize common English contractions/apostrophes before matching,
# so "can't" hits the same keywords as "cannot", etc.
_APOSTROPHE_MAP = {
    "can't": "cannot",
    "won't": "will not",
    "don't": "do not",
    "doesn't": "does not",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "hasn't": "has not",
    "haven't": "have not",
    "hadn't": "had not",
    "couldn't": "could not",
    "wouldn't": "would not",
    "shouldn't": "should not",
}


# Arabic orthographic normalization for keyword matching. Real Arabic
# speech transcripts vary in diacritics, hamza/alef seats, and final-ya/
# ta-marbuta spelling — plain substring matching misses without this
# (the exact fragility called out in this module's LIMITATION note).
# Applied identically to both transcript and keywords, so matching stays
# internally consistent regardless of which form either side uses.
# U+064B–U+0652 harakat (tanween/fatha/damma/kasra/shadda/sukun),
# U+0670 dagger alef, U+0640 tatweel. Escapes used on purpose: literal
# RTL combining chars inside a regex range are unreadable and fragile.
_ARABIC_STRIP = re.compile("[\u064b-\u0652\u0670\u0640]")
_ARABIC_CHAR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا",  # hamza-seated alefs -> bare alef
    "ى": "ي",                       # alef maqsura -> ya
    "ة": "ه",                       # ta marbuta -> ha
})


def _normalize(text: str) -> str:
    """Lowercase, expand contractions, strip stray apostrophes, and
    normalize Arabic orthography (diacritics, alef/ya/ta-marbuta variants)."""
    text = text.lower()
    for contraction, expanded in _APOSTROPHE_MAP.items():
        text = text.replace(contraction, expanded)
    text = text.replace("'", "")
    text = _ARABIC_STRIP.sub("", text)
    text = text.translate(_ARABIC_CHAR_MAP)
    return text


def match_scenario(transcript: str, language: str) -> str | None:
    """
    Returns the matched KB filename (e.g. "KB_Bleeding.yaml") if the
    transcript mentions a known emergency, else None. First match wins
    if multiple scenarios happen to match the same transcript.
    """
    text = _normalize(transcript)

    for path in _all_kb_files():
        kb = _load_yaml(path.name)
        emergency_name = kb.get("emergency", path.stem)

        own_keywords = kb.get("keywords", {}).get(language, [])
        fallback_keywords = KEYWORDS_FALLBACK.get(emergency_name, {}).get(language, [])
        keywords = own_keywords or fallback_keywords

        for kw in keywords:
            if _normalize(kw) in text:
                return path.name

    return None


def get_kb_names() -> list[str]:
    """Returns the `emergency` name for every KB file, for the generic router prompt."""
    return [_load_yaml(path.name).get("emergency", path.stem) for path in _all_kb_files()]


def format_kb_for_prompt(filename: str, language: str) -> str:
    """
    Formats one matched KB file into natural-language instructions for
    the Groq system prompt: triage question, both scenario branches
    with their steps, escalation phrasing, and general-knowledge Q&A
    as a fallback reference.
    """
    kb = _load_yaml(filename)
    emergency_name = kb.get("emergency", filename)

    lines = [f"CURRENT EMERGENCY TOPIC: {emergency_name}"]

    triage = kb.get("triage", {}).get(language)
    if triage:
        lines.append("\nIf you haven't already asked, ask this triage question first:")
        lines.append(f'"{triage["question"]}"')

    scenarios = kb.get("scenarios", {})
    for branch_key, branch_by_lang in scenarios.items():
        branch = branch_by_lang.get(language)
        if not branch:
            continue

        lines.append(f"\nIf the caller's situation is '{branch_key}':")
        for i, step in enumerate(branch.get("steps", []), start=1):
            lines.append(f"{i}. {step}")

        if branch.get("escalate"):
            phrase = branch.get("escalation_phrase", "")
            lines.append(
                f"IMPORTANT — this is a serious case. You must clearly tell "
                f'the caller: "{phrase}"'
            )

        follow_up = branch.get("follow_up")
        if follow_up:
            lines.append(f'After giving these steps, ask: "{follow_up}"')

    general_qa = kb.get("general_knowledge", {}).get(language, [])
    if general_qa:
        lines.append("\nIf the caller asks something related but not covered above, use this reference:")
        for item in general_qa:
            lines.append(f'Q: {item["q"]}')
            lines.append(f'A: {item["a"]}')

    return "\n".join(lines)


def format_generic_router(language: str) -> str:
    """
    Used when no specific emergency has been detected yet. Lists
    available topics without loading their full content, keeping the
    prompt cheap until we actually know what's needed.
    """
    names_str = ", ".join(get_kb_names())

    if language == "ar":
        return (
            f"لم يتم تحديد نوع الطارئة بعد. اسأل المتصل بهدوء عمّا حدث. "
            f"المواضيع المتاحة: {names_str}."
        )
    return (
        f"No specific emergency has been identified yet. Calmly ask the "
        f"caller what happened. Available topics: {names_str}."
    )
