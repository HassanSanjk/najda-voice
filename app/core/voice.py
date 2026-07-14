"""
Turn orchestrator — the "brain" tying STT, LLM, and TTS together per call.

Day 6 status: stability pass. Every external call (STT connect/send,
Groq, TTS, send_audio) is isolated so one failure logs and degrades
gracefully instead of silently killing the background task or the
whole call. Empty/very short transcripts are filtered so stray noise
doesn't trigger a full LLM+TTS round trip.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core import memory
from app.models.schemas import CallSession
from app.prompts.prompt_builder import build_messages
from app.services.deepgram_stt import DeepgramSTTStream
from app.services.deepgram_tts import synthesize
from app.services.groq_llm import stream_completion

logger = logging.getLogger(__name__)

SendAudioFn = Callable[[bytes], Awaitable[None]]

# Keyed by call_sid.
_active_streams: dict[str, DeepgramSTTStream] = {}
_send_audio_callbacks: dict[str, SendAudioFn] = {}

# Transcripts shorter than this (after stripping whitespace) are treated
# as noise/silence artifacts and skipped. This is a rough heuristic, not
# real VAD/silence detection — short real words ("no", "ok") sit right
# at this boundary, so tune it based on what real call testing shows
# rather than trusting this number blindly.
MIN_TRANSCRIPT_LENGTH = 2


async def handle_call_start(session: CallSession, send_audio: SendAudioFn) -> None:
    logger.info(f"[{session.call_sid}] call started")

    stream = DeepgramSTTStream(language=session.language or "en")
    try:
        await stream.connect()
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to open Deepgram STT connection")
        return  # no STT connection means no pipeline for this call — fail silent, not crashed

    _active_streams[session.call_sid] = stream
    _send_audio_callbacks[session.call_sid] = send_audio

    task = asyncio.create_task(_consume_transcripts(session, stream))
    # Fire-and-forget by design, but attach a done-callback so a crash in
    # here gets logged instead of vanishing the way un-awaited asyncio
    # task exceptions normally do.
    task.add_done_callback(lambda t: _log_task_exception(session.call_sid, t))


def _log_task_exception(call_sid: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"[{call_sid}] transcript-consumer task crashed", exc_info=exc)


async def _consume_transcripts(session: CallSession, stream: DeepgramSTTStream) -> None:
    async for transcript in stream.receive_transcripts():
        if not transcript["is_final"]:
            continue  # ignore interim partials, act only on final text

        text = transcript["text"].strip()
        if len(text) < MIN_TRANSCRIPT_LENGTH:
            logger.debug(f"[{session.call_sid}] ignoring short/noise transcript: {text!r}")
            continue

        logger.info(f"[{session.call_sid}] caller said: {text!r}")
        memory.add_turn(session.call_sid, "user", text)

        try:
            await _generate_reply(session)
        except Exception:
            # One bad turn shouldn't end the call — log it and keep
            # listening for what the caller says next.
            logger.exception(f"[{session.call_sid}] failed to generate/send reply")


async def _generate_reply(session: CallSession) -> None:
    """
    Builds the prompt, streams a reply from Groq, synthesizes it via
    Deepgram Aura TTS, and sends the audio back to the caller.

    Groq and TTS failures are isolated from each other: if Groq fails,
    there's no text to speak, so we log and stop. If Groq succeeds but
    TTS fails, the assistant's text turn is still kept in memory (the
    conversation context stays correct even though the caller didn't
    hear it) and the audio failure is logged separately.
    """
    language = session.language or "en"
    history = memory.get_history(session.call_sid)
    messages = build_messages(language, history)

    full_reply = ""
    try:
        async for token in stream_completion(messages):
            full_reply += token
    except Exception:
        logger.exception(f"[{session.call_sid}] Groq completion failed")
        return

    full_reply = full_reply.strip()
    if not full_reply:
        logger.warning(f"[{session.call_sid}] Groq returned an empty reply")
        return

    logger.info(f"[{session.call_sid}] assistant: {full_reply!r}")
    memory.add_turn(session.call_sid, "assistant", full_reply)

    try:
        audio_bytes = await synthesize(full_reply, language=language)
    except Exception:
        logger.exception(f"[{session.call_sid}] TTS synthesis failed")
        return

    send_audio = _send_audio_callbacks.get(session.call_sid)
    if send_audio is None:
        logger.warning(f"[{session.call_sid}] no send_audio callback registered")
        return

    try:
        await send_audio(audio_bytes)
    except Exception:
        # Most likely cause: caller already hung up mid-synthesis.
        logger.exception(f"[{session.call_sid}] failed to send audio back to caller")


async def handle_audio_chunk(session: CallSession, audio_bytes: bytes) -> None:
    stream = _active_streams.get(session.call_sid)
    if stream is None:
        return
    try:
        await stream.send_audio(audio_bytes)
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to forward audio chunk to Deepgram")


async def handle_call_end(session: CallSession) -> None:
    logger.info(f"[{session.call_sid}] call ended")
    stream = _active_streams.pop(session.call_sid, None)
    if stream:
        try:
            await stream.close()
        except Exception:
            logger.exception(f"[{session.call_sid}] error closing Deepgram STT connection")
    _send_audio_callbacks.pop(session.call_sid, None)
    memory.clear(session.call_sid)
