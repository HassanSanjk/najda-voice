"""
Language detection and voice/prompt selection.

STT side (Deepgram Nova-3 Arabic) is well-supported and verified —
dialect codes below are real Deepgram language codes as of their
Nova-3 Arabic launch.

TTS side: Deepgram Aura has no Arabic voice (verified — Aura-2 covers
en, es, nl, fr, de, it, ja only). Day 8 resolves this: English routes
to Deepgram Aura, Arabic routes to ElevenLabs Flash v2.5. See
app/services/deepgram_tts.py and app/services/elevenlabs_tts.py.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Verified Deepgram Nova-3 Arabic dialect codes (STT).
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

# TTS provider per language — resolved as of Day 8.
TTS_PROVIDER_BY_LANGUAGE: dict[str, str | None] = {
    "en": "deepgram_aura",
    "ar": "elevenlabs",
}

# Deepgram Aura voice for English. Confirmed real voice slug.
VOICE_BY_LANGUAGE = {
    "en": "aura-2-asteria-en",
}


def detect_language(deepgram_language_field: str | None) -> str:
    if not deepgram_language_field:
        return DEFAULT_LANGUAGE

    code = deepgram_language_field.strip()
    if code in ARABIC_DIALECT_CODES or code.lower().startswith("ar"):
        return "ar"
    if code.lower().startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def get_tts_provider(language: str) -> str | None:
    return TTS_PROVIDER_BY_LANGUAGE.get(language)


def get_voice_for_language(language: str) -> str:
    if language not in VOICE_BY_LANGUAGE:
        raise ValueError(
            f"No TTS voice configured for language '{language}'. "
            f"Check get_tts_provider() before calling this."
        )
    return VOICE_BY_LANGUAGE[language]


def get_system_prompt_path(language: str) -> Path:
    return PROMPT_FILE_BY_LANGUAGE.get(language, PROMPT_FILE_BY_LANGUAGE[DEFAULT_LANGUAGE])
