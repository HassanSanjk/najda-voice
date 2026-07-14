"""
Turn orchestrator — the "brain" tying STT, LLM, and TTS together per call.

Day 9 status: caller transcripts are now matched against the real KB
YAML files (see app/prompts/kb_loader.py) to detect which emergency
scenario applies, tracked per call so it persists across turns even if
a later transcript doesn't re-mention the keyword (e.g. "yes it still
hurts" won't re-match, but shouldn't need to -- we already know the
scenario from an earlier turn).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core import language, memory
from app.models.schemas import CallSession
from app.prompts import kb_loader
from app.prompts.prompt_builder import build_messages
from app.services import deepgram_tts, elevenlabs_tts
from app.services.deepgram_stt import DeepgramSTTStream
from app.services.groq_llm import stream_completion

logger = logging.getLogger(__name__)

SendAudioFn = Callable[[bytes], Awaitable[None]]

_active_streams: dict[str, DeepgramSTTStream] = {}
_send_audio_callbacks: dict[str, SendAudioFn] = {}
_current_scenario: dict[str, str] = {}  # call_sid -> matched KB filename

MIN_TRANSCRIPT_LENGTH = 2


async def handle_call_start(session: CallSession, send_audio: SendAudioFn) -> None:
    logger.info(f"[{session.call_sid}] call started")

    stream = DeepgramSTTStream(language=session.language or "en")
    try:
        await stream.connect()
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to open Deepgram STT connection")
        return

    _active_streams[session.call_sid] = stream
    _send_audio_callbacks[session.call_sid] = send_audio

    task = asyncio.create_task(_consume_transcripts(session, stream))
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
            continue

        text = transcript["text"].strip()
        if len(text) < MIN_TRANSCRIPT_LENGTH:
            logger.debug(f"[{session.call_sid}] ignoring short/noise transcript: {text!r}")
            continue

        detected_lang = language.detect_language(transcript.get("language"))
        if detected_lang != session.language:
            logger.info(f"[{session.call_sid}] language set to '{detected_lang}'")
            session.language = detected_lang

        # Re-run scenario matching on the latest transcript. If it matches,
        # update (or set) the tracked scenario. If it doesn't match this
        # turn but a scenario was already tracked from an earlier turn,
        # keep using that one -- a caller confirming symptoms ("yes it
        # still hurts") won't re-trigger a keyword match and shouldn't
        # need to.
        matched = kb_loader.match_scenario(text, session.language or "en")
        if matched:
            if _current_scenario.get(session.call_sid) != matched:
                logger.info(f"[{session.call_sid}] scenario matched: {matched}")
            _current_scenario[session.call_sid] = matched

        logger.info(f"[{session.call_sid}] caller said: {text!r}")
        memory.add_turn(session.call_sid, "user", text)

        try:
            await _generate_reply(session)
        except Exception:
            logger.exception(f"[{session.call_sid}] failed to generate/send reply")


async def _generate_reply(session: CallSession) -> None:
    lang = session.language or "en"
    history = memory.get_history(session.call_sid)
    scenario_hint = _current_scenario.get(session.call_sid)
    messages = build_messages(lang, history, scenario_hint)

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

    logger.info(f"[{session.call_sid}] assistant ({lang}, scenario={scenario_hint}): {full_reply!r}")
    memory.add_turn(session.call_sid, "assistant", full_reply)

    try:
        audio_bytes = await _synthesize_speech(full_reply, lang)
    except Exception:
        logger.exception(f"[{session.call_sid}] TTS synthesis failed (provider for '{lang}')")
        return

    send_audio = _send_audio_callbacks.get(session.call_sid)
    if send_audio is None:
        logger.warning(f"[{session.call_sid}] no send_audio callback registered")
        return

    try:
        await send_audio(audio_bytes)
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to send audio back to caller")


async def _synthesize_speech(text: str, lang: str) -> bytes:
    provider = language.get_tts_provider(lang)
    if provider == "deepgram_aura":
        return await deepgram_tts.synthesize(text, language=lang)
    elif provider == "elevenlabs":
        return await elevenlabs_tts.synthesize(text, language=lang)
    else:
        raise ValueError(f"No TTS provider resolved for language '{lang}'")


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
    _current_scenario.pop(session.call_sid, None)
    memory.clear(session.call_sid)
