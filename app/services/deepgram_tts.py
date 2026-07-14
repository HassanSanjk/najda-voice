"""
Deepgram Aura TTS client.

IMPORTANT: Deepgram Aura only supports English, Spanish, Dutch, French,
German, Italian, and Japanese (verified against current docs — see
app/core/language.py for the full note). It has no Arabic voice. This
client only ever handles English; Arabic must route through whatever
provider gets chosen later (still an open decision).

CONFIDENCE NOTE: encoding/sample_rate/container parameter names below
are inferred from strongly consistent cross-source evidence (Deepgram's
REST parameter docs + a Node.js SDK example both use these exact names),
but — unlike deepgram_stt.py, which was verified by directly
introspecting the installed package — I could not introspect
speak.v1.audio.generate()'s actual signature this session (no tool
access). Sanity-check the first real audio output before building
further on top of this: confirm it's raw headerless mu-law (no WAV
header) and plays back cleanly. There's also a known Deepgram-reported
artifact where mulaw+8kHz output has a small click/pop at the very
start of speech — worth listening for, not something to pre-emptively
work around.
"""

from deepgram import AsyncDeepgramClient

from config import settings

SUPPORTED_LANGUAGES = {"en"}

# Deepgram Aura-2 voice. Confirmed real model id.
VOICE_MODEL = "aura-2-asteria-en"

# Twilio Media Streams expects 8kHz mu-law, no container/header.
SAMPLE_RATE = 8000
ENCODING = "mulaw"

_client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)


async def synthesize(text: str, language: str = "en") -> bytes:
    """
    Sends text to Deepgram Aura and returns synthesized audio bytes in
    raw mu-law 8kHz format — intended to be directly playable by Twilio
    Media Streams with no conversion step needed.

    Raises ValueError immediately for unsupported languages instead of
    silently calling Deepgram with a language it doesn't handle.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Deepgram Aura does not support language '{language}'. "
            f"Supported: {SUPPORTED_LANGUAGES}. "
            f"Arabic TTS provider is unresolved — see app/core/language.py."
        )

    response = await _client.speak.v1.audio.generate(
        text=text,
        model=VOICE_MODEL,
        encoding=ENCODING,
        sample_rate=SAMPLE_RATE,
        container="none",  # raw audio, no WAV/OGG wrapper — Twilio needs raw frames
    )
    return response.stream.getvalue()
