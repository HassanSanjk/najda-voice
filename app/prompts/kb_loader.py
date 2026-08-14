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

LIMITATION: matching is word-boundary based, not tokenized, and
deliberately misses inflected/cliticized forms (Arabic definite-article
"الدم", possessives like "قلبي", English "burning" vs keyword "burn")
-- those safely fall through to the generic router rather than
misroute. Misses from diacritics, hamza variants, and spelling
variation are also possible -- worth specifically testing with real
Arabic phrasing, not just the English side, before trusting this in a
demo.
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


# Thresholds for detect_delivered_branches below. Tuned deliberately
# conservative: under-detection only lets the model restate a step the
# caller may already have heard (harmless); over-detection could make it
# skip a safety-critical step the caller never actually heard.
STEP_OVERLAP_THRESHOLD = 0.35
BRANCH_DELIVERY_THRESHOLD = 0.4


def _tokenize(text: str) -> set[str]:
    """Normalize then split into word tokens, dropping sentence punctuation
    (including the Arabic question mark U+061F, so it can never inflate
    token-overlap scores)."""
    return set(re.sub(r"[.,;!?\u061f]", " ", _normalize(text)).split())


def _escalation_phrases(filename: str, language: str) -> set[str]:
    """Normalized escalation phrases across every branch of a KB file.

    Every KB branch lists its escalation phrase AGAIN as its literal last
    step, so raw step strings always include the escalation text. This set
    lets the prompt formatter keep those steps out of the "already given"
    block — escalation is the one thing that must stay repeatable."""
    kb = _load_yaml(filename)
    phrases: set[str] = set()
    for branch_by_lang in kb.get("scenarios", {}).values():
        branch = branch_by_lang.get(language)
        if not branch:
            continue
        phrase = branch.get("escalation_phrase", "")
        if phrase:
            phrases.add(_normalize(phrase))
    return phrases


def detect_delivered_branches(filename: str, language: str, reply_text: str) -> set[str]:
    """
    Returns the step strings from `filename` that the assistant's reply
    plausibly delivered, by word overlap between each step and the reply.

    Granularity is per-branch, not per-scenario: a branch counts as
    delivered only when BRANCH_DELIVERY_THRESHOLD of its steps each contain
    STEP_OVERLAP_THRESHOLD of their words in the reply. This avoids marking
    a scenario "given" off a triage-only turn (e.g. just the severity
    question, which shares few words with any branch's steps) — steps are
    only marked after the model actually spoke enough of one branch's
    content for a human to have heard it.
    """
    kb = _load_yaml(filename)
    reply_words = _tokenize(reply_text)
    delivered: set[str] = set()

    for branch_by_lang in kb.get("scenarios", {}).values():
        branch = branch_by_lang.get(language)
        if not branch:
            continue
        steps = branch.get("steps", [])
        if not steps:
            continue

        qualifying = 0
        for step in steps:
            step_words = _tokenize(step)
            if not step_words:
                continue
            overlap = sum(1 for w in step_words if w in reply_words) / len(step_words)
            if overlap >= STEP_OVERLAP_THRESHOLD:
                qualifying += 1

        if qualifying / len(steps) >= BRANCH_DELIVERY_THRESHOLD:
            delivered.update(steps)

    return delivered


def detect_triage_delivered(filename: str, language: str, reply_text: str) -> str | None:
    """
    Returns the scenario's triage question text if reply_text shows
    meaningful overlap with it, else None. Triage was NOT covered by
    detect_delivered_branches() -- confirmed live: a call whose triage
    answer STT-garbled into something unclassifiable got the identical
    triage question asked twice in a row (word-for-word, hit the TTS
    cache). Tracked the same way as branch steps so it can also be
    marked "already given."
    """
    kb = _load_yaml(filename)
    triage = kb.get("triage", {}).get(language)
    if not triage:
        return None
    question = triage["question"]
    q_words = set(_normalize(question).split())
    if not q_words:
        return None
    reply_words = set(_normalize(reply_text).split())
    overlap = len(q_words & reply_words) / len(q_words)
    return question if overlap >= STEP_OVERLAP_THRESHOLD else None


_KEYWORD_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _keyword_matches(kw: str, normalized_text: str) -> bool:
    """
    Word-boundary match, not raw substring. This exists because a caller
    saying "لا صدمة" (no trauma) got substring-matched on the "دم" (blood)
    keyword -- دم is literally the middle two letters of صدمة -- misrouting
    a plain headache call into KB_Bleeding, which then asked an
    unanswerable triage question for the rest of the call. \b works correctly here because Python's re
    treats Arabic letters as \\w characters by default (Unicode-aware),
    so it still matches "دم" correctly when it IS its own word (e.g.
    "يوجد دم على الجرح"), just not when embedded inside a longer word.
    """
    normalized_kw = _normalize(kw)
    if not normalized_kw:
        return False
    pattern = _KEYWORD_PATTERN_CACHE.get(normalized_kw)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(normalized_kw) + r"\b")
        _KEYWORD_PATTERN_CACHE[normalized_kw] = pattern
    return bool(pattern.search(normalized_text))


def match_scenario(
    transcript: str,
    language: str,
    already_locked: str | None = None,
) -> str | None:
    """
    Returns the matched KB filename (e.g. "KB_Bleeding.yaml") if the
    transcript mentions a known emergency, else None. First match wins
    if multiple scenarios happen to match the same transcript.

    already_locked: the scenario already locked for this call (from the
    caller's first match). Once a scenario has been matched, it is STICKY
    for the rest of the call — a later keyword hit on another scenario
    (e.g. a trauma call where the caller then says "can't breathe",
    matching KB_Choking) must NOT reclassify the emergency. Re-running
    the matcher per utterance without this lock let shared keywords
    hijack an established scenario mid-call.
    """
    if already_locked is not None:
        return already_locked

    text = _normalize(transcript)

    for path in _all_kb_files():
        kb = _load_yaml(path.name)
        emergency_name = kb.get("emergency", path.stem)

        own_keywords = kb.get("keywords", {}).get(language, [])
        fallback_keywords = KEYWORDS_FALLBACK.get(emergency_name, {}).get(language, [])
        keywords = own_keywords or fallback_keywords

        for kw in keywords:
            if _keyword_matches(kw, text):
                return path.name

    return None


def get_kb_names() -> list[str]:
    """Returns the `emergency` name for every KB file, for the generic router prompt."""
    return [_load_yaml(path.name).get("emergency", path.stem) for path in _all_kb_files()]


# ---------------------------------------------------------------------------
# Question-repeat guard: callers who keep failing to answer one question
# must not be asked it again and again in new wording. The triage question
# already has its own guard (detect_triage_delivered); this generalizes to
# every question the assistant asks — triage, branch follow_ups, and ad-hoc
# clarifying questions.
#
# Detection is PURE: it runs on prior assistant turns only, at prompt-build
# time, so the caller can never hear the same question a third time. No
# per-call state is added — nothing to clear, nothing to leak across calls.
#
# Why not plain Jaccard on raw tokens: short Arabic questions are dominated
# by function words (هل/من/في...), so two DIFFERENT questions about the
# same symptom ("...الرجل؟" vs "...اليد؟") outscore a true reworded repeat
# ("...شاحب" vs "...أصفر"). Function words are stripped, and the hard
# override is gated on ASK COUNT (2 prior asks => 3rd intercepted), so a
# residual cross-question match can only trigger a harmless soft nudge —
# never an early escalation. Known limitation: an English reworded repeat
# that swaps the descriptive adjective ("is your leg yellow?" -> "is your
# leg pale?") shares no content tokens and is not clustered here; it's
# covered by the persona's one-question hard limit instead.
# ---------------------------------------------------------------------------

# Function words carrying no topic signal, in both languages (union set —
# one matcher serves ar and en; the other language's words simply never
# appear). Every word left in a token set is genuine content.
_QUESTION_STOPWORDS = {
    # Arabic
    "هل", "من", "في", "على", "إلى", "عند", "عن", "مع", "أن", "إن",
    "لا", "لم", "لن", "ما", "هو", "هي", "هم", "هذا", "هذه", "كان",
    # English
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
    "have", "has", "had", "you", "your", "it", "its", "of", "to", "for",
    "and", "or", "with", "on", "at", "in", "this", "that",
}

# Minimum content-token overlap (intersection / min-side length) for two
# questions to count as the same cluster. Tuned on the observed Arabic
# rewording (see test_question_repeat.py).
QUESTION_SIMILARITY_THRESHOLD = 0.6
# Only the last N assistant turns are examined, so an old question asked
# again much later is treated as new.
QUESTION_WINDOW_TURNS = 6
# Soft nudge: inject the "don't re-ask" prompt block whenever the latest
# assistant turn asked a question (count >= 1) — its job is to prevent the
# 2nd ask from ever being generated.
QUESTION_SOFT_ASKS = 1
# Hard override: once the same cluster has been asked twice, the next
# generation is replaced with the scenario's escalation phrase instead.
QUESTION_HARD_ASKS = 2


def _extract_questions(text: str) -> list[str]:
    """Split an assistant turn's text into its individual questions.

    A sentence counts as a question iff it ends with '?' or '؟'. Statements
    and escalation phrases ("Call emergency services now.") contain no
    question mark and are never extracted — so a stuck-question override
    cannot re-trigger the guard on its own reply.
    """
    return [m.strip() for m in re.findall(r"[^؟?]+[؟?]", text)]


def _question_tokens(question: str) -> set[str]:
    """Token set for clustering: normalized words minus function words."""
    return {w for w in _tokenize(question) if w not in _QUESTION_STOPWORDS}


def _question_overlap(a: str, b: str) -> float:
    """Content-token overlap between two questions: |A∩B| / min(|A|,|B|).

    Min-side normalization (unlike Jaccard) keeps a reworded question that
    PADS extra words — e.g. "...شاحب أو مصفر؟" — clustered with the shorter
    original instead of being diluted by its new tokens.
    """
    ta, tb = _question_tokens(a), _question_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def detect_question_stuck(
    assistant_texts: list[str],
    window: int = QUESTION_WINDOW_TURNS,
) -> dict | None:
    """Return the stuck-question signal for the latest assistant turn, or
    None when there is nothing to report.

    assistant_texts: content of the assistant turns so far this call, in
    order (derived at the call site from memory history). Deliberately a
    pure function of plain strings so the whole guard is unit-testable
    without stubs.

    Only the last `window` turns are examined. The questions from the LATEST
    turn are the candidates; a candidate is matched against every earlier
    question in the window via _question_overlap. Returns
    {"question": <latest wording>, "count": <cluster asks so far, incl. the
    latest turn>, "overrides": <count >= QUESTION_HARD_ASKS>} for the
    candidate with the highest count, else None.
    """
    texts = assistant_texts[-window:] if window else assistant_texts
    if not texts:
        return None

    latest_texts = texts[-1]
    candidates = _extract_questions(latest_texts)
    if not candidates:
        return None

    prior: list[str] = []
    for text in texts[:-1]:
        prior.extend(_extract_questions(text))

    best: tuple[str, int] | None = None
    for cand in candidates:
        count = 1 + sum(1 for q in prior if _question_overlap(cand, q) >= QUESTION_SIMILARITY_THRESHOLD)
        if best is None or count > best[1]:
            best = (cand, count)

    if best is None:
        return None
    question, count = best
    return {
        "question": question,
        "count": count,
        "overrides": count >= QUESTION_HARD_ASKS,
    }


def get_escalation_phrase(filename: str, language: str) -> str | None:
    """The escalation phrase of the first scenario branch flagged `escalate`,
    for the given language.

    Used by the stuck-question hard override so the guard speaks the
    scenario's already-reviewed safety copy instead of new text. Returns
    None when no branch escalates (the caller then gets a generic bilingual
    line)."""
    kb = _load_yaml(filename)
    for branch_by_lang in kb.get("scenarios", {}).values():
        branch = branch_by_lang.get(language)
        if branch and branch.get("escalate"):
            phrase = branch.get("escalation_phrase", "")
            if phrase:
                return phrase
    return None


def format_kb_for_prompt(
    filename: str,
    language: str,
    given_steps: set[str] | None = None,
    current_transcript: str | None = None,
    stuck_question: str | None = None,
) -> str:
    """
    Formats one matched KB file into natural-language instructions for
    the Groq system prompt: triage question, both scenario branches
    with their steps, escalation phrasing, and a general-knowledge Q&A
    fallback.

    given_steps: exact step strings already spoken to the caller this
    call (tracked in core/memory.py). Listed explicitly so the model is
    TOLD it already said them, rather than left to infer that from raw
    conversation history — the direct signal that fixes verbatim
    repetition. Steps whose text equals a branch's escalation_phrase are
    deliberately excluded from the list (escalation is the one thing
    that SHOULD be repeatable).

    current_transcript: the caller's current utterance. Used to trim
    general_knowledge down to the single closest-matching entry instead
    of injecting the full Q&A block unconditionally every turn.

    stuck_question: the caller hasn't answered a question the assistant
    already asked. Tells the model directly (rather than leaving it to
    infer from history) to stop re-asking — the prompt-level soft nudge of
    the generalized question-repeat guard.
    """
    kb = _load_yaml(filename)
    emergency_name = kb.get("emergency", filename)

    lines = [f"CURRENT EMERGENCY TOPIC: {emergency_name}"]

    triage = kb.get("triage", {}).get(language)
    if triage:
        already_asked = given_steps and triage["question"] in given_steps
        if already_asked:
            lines.append(
                "\nYou already asked the triage question below. If the caller's "
                "last answer didn't clearly indicate which situation applies, do "
                "NOT repeat this question word-for-word — ask a short, differently "
                "worded clarifying follow-up instead (e.g. narrow it down, or ask "
                "them to describe what they see/feel):"
            )
        else:
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

    # Anti-repetition state signal: told directly rather than inferred
    # from history. Escalation-identical steps excluded via
    # _escalation_phrases — they're deliberately not suppressed.
    if given_steps:
        escalation = _escalation_phrases(filename, language)
        repeatable = sorted(s for s in given_steps if _normalize(s) not in escalation)
        if repeatable:
            lines.append(
                "\nSteps you have ALREADY told the caller this call — do NOT "
                "repeat these verbatim. If relevant, refer to them briefly "
                '("like I mentioned...") or paraphrase in fewer words instead:'
            )
            for step in repeatable:
                lines.append(f"- {step}")

    # General-knowledge fallback: inject only the closest-matching entry,
    # and only once the caller actually asks something matching. Only
    # substantive tokens (len >= 4 after normalization) count toward a
    # match — short tokens like "is", "a", "do" are ubiquitous in both
    # questions and replies, so a raw overlap test can fire on them alone
    # (e.g. "Hello? Is anyone there?" sharing "is" with a CPR question).
    general_qa = kb.get("general_knowledge", {}).get(language, [])
    if general_qa and current_transcript:
        transcript_words = {w for w in _tokenize(current_transcript) if len(w) >= 4}
        best_match = None
        best_score = 0
        for item in general_qa:
            q_words = {w for w in _tokenize(item["q"]) if len(w) >= 4}
            score = sum(1 for w in q_words if w in transcript_words)
            if score > best_score:
                best_score = score
                best_match = item
        if best_match and best_score > 0:
            lines.append(
                "\nThe caller's question may relate to this reference "
                "(use it as background, in your own words):"
            )
            lines.append(f'Q: {best_match["q"]}')
            lines.append(f'A: {best_match["a"]}')

    # Soft nudge of the question-repeat guard: the caller hasn't answered
    # a question already asked. Explicit instruction (not left to the
    # model's inference) so a reworded re-ask is suppressed at the source.
    if stuck_question:
        lines.append(
            f'\nYou recently asked the caller: "{stuck_question}". If they '
            "have already answered it, move on and do NOT ask it again, "
            "reworded or not. If they still have not given a clear answer, "
            "ask a short plain yes/no version once more at most — then move "
            "on or, if they still won't answer, say the escalation phrase "
            "from your instructions."
        )

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
