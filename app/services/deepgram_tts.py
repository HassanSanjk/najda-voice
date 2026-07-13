"""
Deepgram Aura TTS client.

Interface finalized now; implementation lands Day 5.

IMPORTANT: Deepgram Aura only supports English, Spanish, Dutch, French,
German, Italian, and Japanese (verified against current docs). It has
no Arabic voice. This client should only ever be called for English.
Arabic responses must route through whatever provider gets chosen
later (see app/core/language.py TTS_PROVIDER_BY_LANGUAGE) — do not
add an "ar" branch here until that decision is made.
"""

from config import settings

SUPPORTED_LANGUAGES = {"en"}  # extend only if Aura adds more AND we confirm it


async def synthesize(text: str, language: str = "en") -> bytes:
    """
    Sends text to Deepgram Aura and returns synthesized audio bytes
    in mu-law 8kHz format (matching Twilio Media Streams' expected format).

    Raises ValueError immediately for unsupported languages instead of
    silently calling Deepgram with a language it doesn't handle.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Deepgram Aura does not support language '{language}'. "
            f"Supported: {SUPPORTED_LANGUAGES}. "
            f"Arabic TTS provider is unresolved — see app/core/language.py."
        )
    raise NotImplementedError("Implemented Day 5")
