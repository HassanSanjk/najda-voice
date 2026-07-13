"""
Turn orchestrator — the "brain" tying STT, LLM, and TTS together per call.

Function signatures are finalized now so app/routes/voice.py has a stable
interface to call into. Bodies are implemented incrementally:
    handle_call_start   -> Day 3 (opens Deepgram STT connection)
    handle_audio_chunk  -> Days 3-7 (STT -> LLM -> TTS pipeline)
    handle_call_end     -> Day 6 (cleanup, close connections)
"""

from app.core import language, memory
from app.models.schemas import CallSession


async def handle_call_start(session: CallSession) -> None:
    """
    Called once when Twilio's 'start' event arrives.
    Will open a streaming Deepgram STT connection for this call.
    """
    print(f"[voice] call started: {session.call_sid}")
    # TODO Day 3: open Deepgram STT stream, store connection on session


async def handle_audio_chunk(session: CallSession, audio_bytes: bytes) -> bytes | None:
    """
    Called for every inbound audio chunk from the caller.

    Eventually:
        1. Forward audio_bytes to the open Deepgram STT stream
        2. When a final transcript arrives, detect/update session.language
        3. Build prompt via prompt_builder using session.language + memory history
        4. Stream response from Groq
        5. Resolve TTS provider via language.get_tts_provider(session.language) —
           if it's None (currently true for "ar"), this call cannot produce
           audio yet and must be handled explicitly, not silently skipped.
        6. Synthesize response via the resolved provider
        7. Save the turn to memory
        8. Return the resulting audio bytes to be sent back to the caller

    Returns None for now (no reply audio yet — pipeline not implemented).
    """
    # TODO Days 3-7: implement full pipeline. When wiring TTS, remember:
    #   provider = language.get_tts_provider(session.language)
    #   if provider is None: handle gap (fallback to English? error message?)
    return None


async def handle_call_end(session: CallSession) -> None:
    """
    Called when Twilio's 'stop' event arrives or the socket disconnects.
    Will close the Deepgram STT connection and flush any final memory state.
    """
    print(f"[voice] call ended: {session.call_sid}")
    memory.clear(session.call_sid)
    # TODO Day 6: also close the open STT connection stored on session
