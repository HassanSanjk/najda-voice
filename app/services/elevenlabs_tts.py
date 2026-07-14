"""
ElevenLabs TTS client — used for Arabic responses only.

Chosen specifically for Arabic (Day 8 decision) since Deepgram Aura has
no Arabic voice at all (see deepgram_tts.py / core/language.py). Flash
v2.5 was chosen over faster alternatives — Cartesia is faster but has
weak Arabic support, the wrong tradeoff here. Voxtral TTS scored higher
in blind Arabic-speaker preference tests but requires self-hosting,
ruled out given this project's timeline; flagged as a possible future
upgrade, not pursued now.

Verified against ElevenLabs' current docs + Python SDK examples:
- output_format="ulaw_8000" is a real, documented literal, purpose-built
  for Twilio telephony — no audio conversion needed, same as the
  Deepgram STT leg.
- model_id="eleven_flash_v2_5" is a real, current model id.
- language_code (ISO 639-1, e.g. "ar") enforces the target language.
- client.text_to_speech.convert(...) on AsyncElevenLabs is awaited and
  returns an ASYNC ITERATOR of audio byte chunks, not a single bytes
  blob — this implementation collects all chunks before returning, to
  match deepgram_tts.synthesize()'s single-bytes-return interface.

CONFIDENCE NOTE: confirmed via multiple independent current sources
(ElevenLabs' own docs + SDK examples), not verified by direct
introspection the way deepgram_stt.py was (no tool access this
session). Sanity-check the very first real audio output.
"""

from elevenlabs.client import AsyncElevenLabs

from config import settings

MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = "ulaw_8000"  # matches Twilio's native format directly

_client = AsyncElevenLabs(api_key=settings.elevenlabs_api_key)


async def synthesize(text: str, language: str = "ar") -> bytes:
    """
    Sends text to ElevenLabs Flash v2.5 and returns synthesized audio
    bytes in raw mu-law 8kHz format, directly playable by Twilio Media
    Streams.

    Requires ELEVENLABS_VOICE_ID_AR to be set. There's no safe default
    to hardcode — Arabic voice quality varies significantly by voice,
    and picking one requires browsing ElevenLabs' voice library
    (elevenlabs.io -> Voices -> filter by language) and copying its ID.
    """
    if not settings.elevenlabs_voice_id_ar:
        raise RuntimeError(
            "ELEVENLABS_VOICE_ID_AR is not set. Pick an Arabic-appropriate "
            "voice from the ElevenLabs voice library and copy its voice_id "
            "into .env before Arabic calls can produce audio."
        )

    audio_stream = await _client.text_to_speech.convert(
        text=text,
        voice_id=settings.elevenlabs_voice_id_ar,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
        language_code=language,
    )

    chunks = []
    async for chunk in audio_stream:
        chunks.append(chunk)
    return b"".join(chunks)
