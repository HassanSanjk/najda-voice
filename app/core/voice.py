"""
Turn orchestrator — the "brain" tying STT, LLM, and TTS together per call.

Day 10 status: Groq's reply is now streamed and synthesized sentence by
sentence, instead of buffering the full reply before any TTS happens.
Each complete sentence is spoken as soon as it's ready, so the caller
starts hearing a response while Groq is still generating the rest of it.

This is a sequential chunked approach (synthesize+send sentence N, THEN
resume reading Groq's stream for sentence N+1), not a fully pipelined
queue-based version that would let generation and synthesis overlap
concurrently. The sequential version already delivers the actual goal
("start speaking before the full response is generated") with far less
complexity — flagged as a possible future optimization, not pursued now.
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

# Sentence-ending punctuation, English and Arabic. Plain substring
# detection, not real sentence tokenization -- fine for short first-aid
# replies, but abbreviations or decimals containing "." could split
# early. Not a concern at this reply length/style, worth revisiting if
# replies get more complex later.
SENTENCE_ENDINGS = {".", "!", "?", "؟"}

# Safety valve: if Groq goes this many characters without producing
# sentence-ending punctuation, flush anyway rather than silently
# reverting to "wait for the whole thing" behavior.
MAX_BUFFER_BEFORE_FORCED_FLUSH = 200


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


def _extract_complete_sentences(buffer: str) -> tuple[list[str], str]:
    """
    Splits buffer into complete sentences (ending in one of
    SENTENCE_ENDINGS) and returns (complete_sentences, remainder_still_in_progress).
    """
    sentences = []
    current = ""
    for char in buffer:
        current += char
        if char in SENTENCE_ENDINGS:
            sentences.append(current.strip())
            current = ""

    if not sentences and len(current) > MAX_BUFFER_BEFORE_FORCED_FLUSH:
        sentences.append(current.strip())
        current = ""

    return sentences, current


async def _generate_reply(session: CallSession) -> None:
    """
    Builds the prompt, then streams Groq's reply sentence by sentence:
    each complete sentence is synthesized and sent to the caller
    immediately, rather than waiting for the full reply before any
    audio goes out.

    If Groq fails partway through, whatever sentences were already
    spoken are still saved to memory as a partial reply -- better than
    discarding a partially-delivered turn the caller already heard.
    """
    lang = session.language or "en"
    history = memory.get_history(session.call_sid)
    scenario_hint = _current_scenario.get(session.call_sid)
    messages = build_messages(lang, history, scenario_hint)

    buffer = ""
    full_reply_parts: list[str] = []

    try:
        async for token in stream_completion(messages):
            buffer += token
            complete_sentences, buffer = _extract_complete_sentences(buffer)
            for sentence in complete_sentences:
                if not sentence:
                    continue
                full_reply_parts.append(sentence)
                await _speak_chunk(session, sentence, lang)
    except Exception:
        logger.exception(f"[{session.call_sid}] Groq completion failed mid-stream")
        # Fall through deliberately -- flush/save whatever was already
        # generated and spoken rather than losing it.

    trailing = buffer.strip()
    if trailing:
        full_reply_parts.append(trailing)
        await _speak_chunk(session, trailing, lang)

    full_reply = " ".join(full_reply_parts).strip()
    if not full_reply:
        logger.warning(f"[{session.call_sid}] Groq returned an empty reply")
        return

    logger.info(f"[{session.call_sid}] assistant ({lang}, scenario={scenario_hint}): {full_reply!r}")
    memory.add_turn(session.call_sid, "assistant", full_reply)


async def _speak_chunk(session: CallSession, text: str, lang: str) -> None:
    """
    Synthesizes one sentence-sized chunk and sends it to the caller
    right away. Isolated in its own try/except so a transient TTS
    failure on one sentence doesn't lose the rest of the reply --
    subsequent sentences still get a chance to play.
    """
    try:
        audio_bytes = await _synthesize_speech(text, lang)
    except Exception:
        logger.exception(f"[{session.call_sid}] TTS synthesis failed for chunk: {text!r}")
        return

    send_audio = _send_audio_callbacks.get(session.call_sid)
    if send_audio is None:
        logger.warning(f"[{session.call_sid}] no send_audio callback registered")
        return

    try:
        await send_audio(audio_bytes)
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to send audio chunk back to caller")


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
