"""
Deepgram Aura TTS client.

IMPORTANT: Deepgram Aura only supports English, Spanish, Dutch, French,
German, Italian, and Japanese (verified against current docs — see
app/core/language.py for the full note). It has no Arabic voice. This
client only ever handles English; Arabic must route through whatever
provider gets chosen later (still an open decision).

Verified against installed deepgram-sdk 7.4.0: generate() is an async
generator yielding raw bytes chunks, not an awaitable — must be consumed
with `async for`, not `await`.
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

    # generate() is an async generator that yields bytes chunks —
    # must be iterated with `async for`, not `await`.
    chunks: list[bytes] = []
    async for chunk in _client.speak.v1.audio.generate(
        text=text,
        model=VOICE_MODEL,
        encoding=ENCODING,
        sample_rate=SAMPLE_RATE,
        container="none",  # raw audio, no WAV/OGG wrapper — Twilio needs raw frames
    ):
        chunks.append(chunk)
    return b"".join(chunks)
