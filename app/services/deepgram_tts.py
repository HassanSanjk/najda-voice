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

import asyncio
import logging

from deepgram import AsyncDeepgramClient

from config import settings

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {"en"}

# Cap concurrent Aura requests. A single long/duplicated reply firing many
# sentences at once tripped Deepgram's TTS rate limit in live testing
# (429 "Please try again later" x3, sentences lost). Playback runs at 1x
# (~3s/sentence) while synthesis takes ~1-2s, so modest serialization is
# inaudible to the caller.
_concurrency = asyncio.Semaphore(3)

# Deepgram Aura-2 voice for English. Read from env (DEEPGRAM_TTS_MODEL_EN);
# keep in sync with language.VOICE_BY_LANGUAGE — both read this setting.
VOICE_MODEL = settings.deepgram_tts_model_en

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
    async with _concurrency:
        generator = _client.speak.v1.audio.generate(
            text=text,
            model=VOICE_MODEL,
            encoding=ENCODING,
            sample_rate=SAMPLE_RATE,
            container="none",  # raw audio, no WAV/OGG wrapper — Twilio needs raw frames
        )
        try:
            async for chunk in generator:
                chunks.append(chunk)
        except Exception as exc:
            status = getattr(exc, "status_code", getattr(exc, "status", "?"))
            logger.warning(f"[deepgram_tts] TTS request failed status={status}: {type(exc).__name__}")
            raise
        finally:
            # Deterministic close on cancellation instead of relying on
            # async-generator refcount GC — prevents dangling pooled
            # connections when a barge-in cancels a mid-stream reply.
            await generator.aclose()
    return b"".join(chunks)
