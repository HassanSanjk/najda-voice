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
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"

# Used only for KB files that don't define their own `keywords` field.
# Not exhaustive -- expand based on what real callers actually say.
KEYWORDS_FALLBACK = {
    "bleeding": {
        "en": ["bleed", "bleeding", "blood", "cut", "wound", "gash"],
        "ar": ["نزيف", "دم", "جرح", "قطع"],
    },
    "burns": {
        "en": ["burn", "burned", "burnt", "scald", "fire"],
        "ar": ["حرق", "حروق", "احتراق"],
    },
    "choking": {
        "en": ["choke", "choking", "can't breathe", "stuck in throat"],
        "ar": ["اختناق", "شرقة", "لا يستطيع التنفس"],
    },
    "cpr": {
        "en": ["not breathing", "no pulse", "unconscious", "cpr", "heart stopped"],
        "ar": ["لا يتنفس", "فاقد الوعي", "قلبه توقف", "إنعاش"],
    },
    "electric_shock": {
        "en": ["electric shock", "electrocuted", "shocked", "power line"],
        "ar": ["صعقة كهربائية", "صعق", "كهرباء"],
    },
    "fractures": {
        "en": ["broken bone", "fracture", "broken arm", "broken leg"],
        "ar": ["كسر", "عظمة مكسورة"],
    },
    "snake_bites": {
        "en": ["snake bite", "snake bit", "bitten by a snake"],
        "ar": ["لدغة ثعبان", "عضة ثعبان"],
    },
    "allergic_reactions": {
        "en": ["allergic", "allergy", "anaphylaxis", "swelling face", "hives"],
        "ar": ["حساسية", "تحسس", "تورم الوجه"],
    },
}


@lru_cache(maxsize=None)
def _load_yaml(filename: str) -> dict:
    path = KNOWLEDGE_DIR / filename
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def _all_kb_files() -> list[Path]:
    return sorted(KNOWLEDGE_DIR.glob("KB_*.yaml"))


def match_scenario(transcript: str, language: str) -> str | None:
    """
    Returns the matched KB filename (e.g. "KB_Bleeding.yaml") if the
    transcript mentions a known emergency, else None. First match wins
    if multiple scenarios happen to match the same transcript.
    """
    text = transcript.lower()

    for path in _all_kb_files():
        kb = _load_yaml(path.name)
        emergency_name = kb.get("emergency", path.stem)

        own_keywords = kb.get("keywords", {}).get(language, [])
        fallback_keywords = KEYWORDS_FALLBACK.get(emergency_name, {}).get(language, [])
        keywords = own_keywords or fallback_keywords

        for kw in keywords:
            if kw.lower() in text:
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
