"""
Groq-hosted Orpheus TTS client — Arabic (Saudi dialect) AND English speech.

Why this exists (July 2026): ElevenLabs was the original Arabic TTS
choice, but its free tier rejects *library* voices via API (402
paid_plan_required — confirmed in live testing), which left Arabic
callers in silence. Orpheus runs on the SAME Groq account/key this
project already uses for its LLM — no extra account, no extra env vars,
and $40/1M characters on paid tiers (a full call is a fraction of a
cent). ElevenLabs remains selectable via TTS_PROVIDER_AR=elevenlabs.

Since Jan 2026 Groq also hosts an English Orpheus model
(canopylabs/orpheus-v1-english, $22/1M chars) — English TTS was
consolidated onto it (Aug 2026) so both languages share ONE provider,
client, conversion path and concurrency policy, replacing Deepgram Aura.
Deepgram remains for STT only.

Verified against Groq's current docs and the installed groq SDK (1.5.0):
- Arabic: "canopylabs/orpheus-arabic-saudi" — Groq's supported replacement
  for the deprecated playai-tts-arabic. Six voices: abdullah, fahad,
  sultan (male); lulwa, noura, aisha (female).
- English: "canopylabs/orpheus-v1-english" — six voices, vocal-direction
  tags supported.
- Orpheus supports ONLY response_format="wav" (the SDK's "mulaw" literal
  applies to the legacy playai models). We convert WAV -> PCM -> 8kHz
  mu-law locally with audioop — the audioop-lts backport in
  requirements.txt exists for exactly this (audioop left the stdlib in
  Python 3.13, PEP 594).
- Input is capped at 200 characters per request. Longer text is split at
  word boundaries and synthesized sequentially in order. (Sentence-level
  concurrency already happens a layer up, in app/core/voice.py.)
- await client.audio.speech.create(...) returns AsyncBinaryAPIResponse;
  the bytes come from `await response.read()`.
"""

import asyncio
import audioop  # stdlib < 3.13; audioop-lts backport on 3.13+
import io
import logging
import wave

from groq import AsyncGroq

from config import settings

logger = logging.getLogger(__name__)

MODEL = "canopylabs/orpheus-arabic-saudi"  # Arabic (legacy name — imported by scripts)
ENGLISH_MODEL = settings.groq_tts_model_en  # "canopylabs/orpheus-v1-english" (also env-overridable)
DEFAULT_VOICE = "aisha"  # Arabic default — professional/clear female
DEFAULT_VOICE_EN = "austin"  # English default — Groq docs example voice (GROQ_TTS_VOICE_EN overrides)

TARGET_RATE = 8000  # Telnyx PCMU
MAX_INPUT_CHARS = 200  # hard Orpheus API limit per request (both models)

_client = AsyncGroq(api_key=settings.groq_api_key)

# Free-tier Orpheus has a small per-minute token budget (observed: 1200/min,
# ~1 token per character). The reply pipeline fires every sentence's TTS
# concurrently, and a 4-5 sentence burst instantly trips 429 storms — each
# then waiting through 6s retry-after backoffs (observed live: sentence
# synthesis ballooning to 6-13s). Serializing the requests spreads the burst;
# it costs nearly nothing perceptually because the caller hears audio at 1x
# speed (~3s/sentence) while synthesis takes ~0.5-1s — sentence N+1's synth
# hides behind sentence N's playback either way. (2 concurrent still tripped
# 429s in live testing when turns overlapped; 1 is the sweet spot on free tier.)
# On the paid Developer tier (10x limits), raise GROQ_TTS_CONCURRENCY to ~3.
_concurrency = asyncio.Semaphore(max(1, min(int(settings.groq_tts_concurrency or 1), 8)))


def _split_text(text: str, limit: int = MAX_INPUT_CHARS) -> list[str]:
    """Splits text into <=limit-char pieces at word boundaries (hard-splits
    only a single word longer than the limit, which real speech never has)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    current = ""
    for word in text.split():
        if not current:
            candidate = word
        else:
            candidate = f"{current} {word}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        while len(word) > limit:  # pathological single word
            pieces.append(word[:limit])
            word = word[limit:]
        current = word
    if current:
        pieces.append(current)
    return pieces


def _wav_to_mulaw_8k(wav_bytes: bytes) -> bytes:
    """Decodes a PCM WAV of any common rate/channels/width into raw
    headerless 8kHz mono mu-law, the format Telnyx plays directly."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())

    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if rate != TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TARGET_RATE, None)
    return audioop.lin2ulaw(pcm, 2)


def _model_for(language: str) -> str:
    """Returns the Orpheus model for the language (Arabic or English)."""
    return MODEL if language == "ar" else ENGLISH_MODEL


def _voice_for(language: str) -> str:
    """Returns the configured Orpheus voice for the language."""
    if language == "ar":
        return settings.groq_tts_voice_ar or DEFAULT_VOICE
    return settings.groq_tts_voice_en or DEFAULT_VOICE_EN


async def synthesize(text: str, language: str = "ar") -> bytes:
    """Synthesizes speech via Groq Orpheus and returns raw mu-law 8kHz
    bytes. Arabic routes to `orpheus-arabic-saudi`, English to
    `orpheus-v1-english` — identical API shape, so both share the same
    WAV->mulaw conversion and 200-char input cap."""
    model = _model_for(language)
    voice = _voice_for(language)

    mulaw_parts: list[bytes] = []
    for piece in _split_text(text):
        if not any(ch.isalnum() for ch in piece):
            continue  # API rejects letterless input (400) — e.g. a stray ")"
        async with _concurrency:
            try:
                response = await _client.audio.speech.create(
                    model=model,
                    voice=voice,
                    input=piece,
                    response_format="wav",
                )
            except Exception as exc:
                status = getattr(exc, "status_code", getattr(exc, "status", "?"))
                logger.warning(f"[groq_tts] TTS request failed status={status}: {type(exc).__name__}")
                raise
            try:
                wav_bytes = await response.read()
            finally:
                # read() auto-closes on completion; this covers cancellation /
                # early-exit paths so the pooled connection isn't left dangling.
                if not response.is_closed:
                    await response.close()
        mulaw_parts.append(_wav_to_mulaw_8k(wav_bytes))
    return b"".join(mulaw_parts)
