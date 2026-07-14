"""
ElevenLabs TTS client — used for Arabic responses only.

Chosen specifically for Arabic (Day 8 decision) since Deepgram Aura has
no Arabic voice at all (see deepgram_tts.py / core/language.py). Flash
v2.5 was chosen over faster alternatives — Cartesia is faster but has
weak Arabic support, the wrong tradeoff here. Voxtral TTS scored higher
in blind Arabic-speaker preference tests but requires self-hosting,
ruled out given this project's timeline; flagged as a possible future
upgrade, not pursued now.

Verified against installed elevenlabs SDK: convert() is an async
generator yielding raw bytes chunks (same pattern as Deepgram's
generate() — the SDK's own docstring example shows `await` on it,
which is wrong). Must be iterated with `async for`, not `await`.
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

    # convert() is an async generator (same pattern as Deepgram's
    # generate()) — must be iterated with `async for`, not `await`.
    chunks = []
    async for chunk in _client.text_to_speech.convert(
        text=text,
        voice_id=settings.elevenlabs_voice_id_ar,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
        language_code=language,
    ):
        chunks.append(chunk)
    return b"".join(chunks)
