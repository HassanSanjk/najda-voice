"""
Language detection and voice/prompt selection.

STT side (Deepgram Nova-3 Arabic) is well-supported and verified —
dialect codes below are real Deepgram language codes as of their
Nova-3 Arabic launch.

TTS side has an open gap: Deepgram Aura does not support Arabic
(confirmed against current docs — Aura-2 covers en, es, nl, fr, de,
it, ja only). Arabic TTS provider is UNDECIDED. Do not wire a real
call to Aura with an Arabic voice — it will fail. This is stubbed
deliberately so the gap stays visible instead of silently broken.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Verified Deepgram Nova-3 Arabic dialect codes (STT).
# Source: Deepgram Nova-3 Arabic launch docs.
ARABIC_DIALECT_CODES = {
    "ar", "ar-AE", "ar-SA", "ar-QA", "ar-KW", "ar-SY", "ar-LB", "ar-PS",
    "ar-JO", "ar-EG", "ar-SD", "ar-MA", "ar-DZ", "ar-TN", "ar-IQ",
    "ar-TD", "ar-IR",
}

DEFAULT_LANGUAGE = "en"

PROMPT_FILE_BY_LANGUAGE = {
    "en": PROMPTS_DIR / "system_en.txt",
    "ar": PROMPTS_DIR / "system_ar.txt",
}

# TTS provider per language. "ar" is intentionally None — no decision
# made yet. Whatever calls this must handle None explicitly rather
# than assume a working voice exists.
TTS_PROVIDER_BY_LANGUAGE: dict[str, str | None] = {
    "en": "deepgram_aura",
    "ar": None,  # TODO: decide — ElevenLabs / Azure / Google Neural TTS
}

# Deepgram Aura voice for English. Confirmed real voice slug.
VOICE_BY_LANGUAGE = {
    "en": "aura-2-asteria-en",
}


def detect_language(deepgram_language_field: str | None) -> str:
    """
    Maps Deepgram's detected language code to our supported set ("en" / "ar").
    Falls back to DEFAULT_LANGUAGE for anything unrecognized.
    """
    if not deepgram_language_field:
        return DEFAULT_LANGUAGE

    code = deepgram_language_field.strip()
    if code in ARABIC_DIALECT_CODES or code.lower().startswith("ar"):
        return "ar"
    if code.lower().startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def get_tts_provider(language: str) -> str | None:
    """
    Returns the TTS provider key for a language, or None if unresolved.
    Callers (app/core/voice.py) MUST check for None and handle it —
    e.g. by falling back to English, or surfacing a clear error —
    rather than calling a TTS API with a language it doesn't support.
    """
    return TTS_PROVIDER_BY_LANGUAGE.get(language)


def get_voice_for_language(language: str) -> str:
    """
    Only valid for languages with a resolved TTS provider.
    Raises if called for a language without one (e.g. "ar" today).
    """
    if language not in VOICE_BY_LANGUAGE:
        raise ValueError(
            f"No TTS voice configured for language '{language}'. "
            f"Check get_tts_provider() before calling this."
        )
    return VOICE_BY_LANGUAGE[language]


def get_system_prompt_path(language: str) -> Path:
    return PROMPT_FILE_BY_LANGUAGE.get(language, PROMPT_FILE_BY_LANGUAGE[DEFAULT_LANGUAGE])
