"""
Language detection and voice/prompt selection.

STT side (Deepgram Nova-3 Arabic) is well-supported and verified —
dialect codes below are real Deepgram language codes as of their
Nova-3 Arabic launch.

TTS side: Groq Orpheus covers both languages (Arabic Saudi dialect +
English). English consolidated onto it Aug 2026, replacing Deepgram Aura
(which has no Arabic voice — verified; Aura-2 covers en, es, nl, fr, de,
it, ja only). See app/services/groq_tts.py.
"""

from pathlib import Path

from config import settings  # shared voice config — see VOICE_BY_LANGUAGE below

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

# TTS provider per language. English was Deepgram Aura until Aug 2026; it
# now routes to Groq Orpheus English (canopylabs/orpheus-v1-english) so
# both languages share one provider/code path — see app/services/groq_tts.py.
# Arabic reworked July 2026: ElevenLabs' free tier rejects library voices
# via API (402), so the default Arabic provider is Groq-hosted Orpheus.
# ElevenLabs remains selectable via TTS_PROVIDER_AR=elevenlabs.
TTS_PROVIDER_BY_LANGUAGE: dict[str, str | None] = {
    "en": "groq_orpheus",
    "ar": None,  # resolved dynamically from settings — see get_tts_provider()
}

_AR_PROVIDERS = {
    "groq": "groq_orpheus",
    "elevenlabs": "elevenlabs",
}

# Orpheus voice per language. Read from env (GROQ_TTS_VOICE_AR / GROQ_TTS_VOICE_EN);
# defaults live in groq_tts.DEFAULT_VOICE / DEFAULT_VOICE_EN.
VOICE_BY_LANGUAGE = {
    "ar": settings.groq_tts_voice_ar or "aisha",
    "en": settings.groq_tts_voice_en or "austin",
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
    if language == "ar":
        from config import settings  # local import to avoid config<->language cycles
        return _AR_PROVIDERS.get(settings.tts_provider_ar.strip().lower(), "groq_orpheus")
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
