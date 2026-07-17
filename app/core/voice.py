"""
Turn orchestrator — the "brain" tying STT, LLM, and TTS together per call.

Changes:
- Responsiveness fix: TTS synthesis for each sentence now runs decoupled
  from actually sending/pacing that sentence's audio. A dedicated per-call
  sender task pulls finished audio off a queue and paces it out to Telnyx;
  the reply-generation coroutine no longer blocks on real-time playback
  duration before starting the next sentence's synthesis. This was the
  single largest self-imposed latency source in the pipeline -- previously,
  synthesizing sentence N+1 didn't start until sentence N's audio had
  *finished playing* (several real seconds per sentence), not just
  finished synthesizing.
- Barge-in: a new caller utterance arriving while Najda is still
  generating/speaking a reply cancels the in-progress reply and drops
  any audio already queued but not yet sent.
- speech_final never fired reliably in testing; "UtteranceEnd" (a message
  type, not a distinct EventType) is the real end-of-utterance signal,
  with a timeout-based safety net in case it doesn't fire.
"""

import asyncio
import logging
import time
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
_current_scenario: dict[str, str] = {}
_transcript_tasks: dict[str, asyncio.Task] = {}
_utterance_buffers: dict[str, list[str]] = {}
_last_fragment_time: dict[str, float] = {}
_audio_queues: dict[str, asyncio.Queue] = {}
_sender_tasks: dict[str, asyncio.Task] = {}
_reply_tasks: dict[str, asyncio.Task] = {}

MIN_TRANSCRIPT_LENGTH = 2
FRAGMENT_TIMEOUT_S = 3.0

SENTENCE_ENDINGS = {".", "!", "?", "؟"}
MAX_BUFFER_BEFORE_FORCED_FLUSH = 200


async def handle_call_start(session: CallSession, send_audio: SendAudioFn) -> None:
    logger.info(f"[{session.call_sid}] call started")

    stream = DeepgramSTTStream(language=session.language or "en")
    try:
        await stream.connect()
    except Exception:
        logger.exception(f"[{session.call_sid}] failed to open Deepgram STT connection")
        return

    call_sid = session.call_sid
    _active_streams[call_sid] = stream
    _send_audio_callbacks[call_sid] = send_audio
    _utterance_buffers[call_sid] = []
    _last_fragment_time[call_sid] = 0.0
    _audio_queues[call_sid] = asyncio.Queue()

    sender_task = asyncio.create_task(_audio_sender_loop(call_sid))
    _sender_tasks[call_sid] = sender_task

    task = asyncio.create_task(_consume_transcripts(session, stream))
    task.add_done_callback(lambda t: _log_task_exception(call_sid, t))
    _transcript_tasks[call_sid] = task


async def _audio_sender_loop(call_sid: str) -> None:
    queue = _audio_queues[call_sid]

    while True:
        chunk = await queue.get()
        if chunk is None:
            break

        send_audio = _send_audio_callbacks.get(call_sid)
        if send_audio is None:
            continue

        try:
            await send_audio(chunk)
        except Exception:
            logger.exception(f"[{call_sid}] failed to send audio chunk from sender loop")


def _log_task_exception(call_sid: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"[{call_sid}] transcript-consumer task crashed", exc_info=exc)


def _log_reply_task_exception(call_sid: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"[{call_sid}] reply-generation task crashed", exc_info=exc)


async def _consume_transcripts(session: CallSession, stream: DeepgramSTTStream) -> None:
    call_sid = session.call_sid
    last_language: str | None = None

    async for transcript in stream.receive_transcripts():
        if transcript.get("flush"):
            await _flush_utterance(session, last_language)
            continue

        if not transcript.get("is_final"):
            continue

        fragment = transcript["text"].strip()
        if fragment:
            _utterance_buffers.setdefault(call_sid, []).append(fragment)
            _last_fragment_time[call_sid] = time.monotonic()
            if transcript.get("language"):
                last_language = transcript["language"]

        buffered = _utterance_buffers.get(call_sid, [])
        if buffered and (time.monotonic() - _last_fragment_time.get(call_sid, 0)) > FRAGMENT_TIMEOUT_S:
            logger.warning(f"[{call_sid}] no UtteranceEnd received, flushing on timeout")
            await _flush_utterance(session, last_language)


async def _flush_utterance(session: CallSession, detected_language_code: str | None) -> None:
    call_sid = session.call_sid
    utterance_received_at = time.monotonic()
    full_text = " ".join(_utterance_buffers.get(call_sid, [])).strip()
    _utterance_buffers[call_sid] = []

    if len(full_text) < MIN_TRANSCRIPT_LENGTH:
        if full_text:
            logger.debug(f"[{call_sid}] ignoring short/noise utterance: {full_text!r}")
        return

    existing_reply = _reply_tasks.get(call_sid)
    if existing_reply and not existing_reply.done():
        logger.info(f"[{call_sid}] barge-in detected, cancelling in-progress reply")
        existing_reply.cancel()
        try:
            await existing_reply
        except asyncio.CancelledError:
            pass
        await _drain_audio_queue(call_sid)

    detected_lang = language.detect_language(detected_language_code)
    if detected_lang != session.language:
        logger.info(f"[{call_sid}] language set to '{detected_lang}'")
        session.language = detected_lang

    matched = kb_loader.match_scenario(full_text, session.language or "en")
    if matched:
        if _current_scenario.get(call_sid) != matched:
            logger.info(f"[{call_sid}] scenario matched: {matched}")
        _current_scenario[call_sid] = matched

    logger.info(f"[{call_sid}] caller said: {full_text!r}")
    memory.add_turn(call_sid, "user", full_text)

    reply_task = asyncio.create_task(_generate_reply(session, utterance_received_at))
    reply_task.add_done_callback(lambda t: _log_reply_task_exception(call_sid, t))
    _reply_tasks[call_sid] = reply_task


async def _drain_audio_queue(call_sid: str) -> None:
    queue = _audio_queues.get(call_sid)
    if not queue:
        return
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


def _extract_complete_sentences(buf: str) -> tuple[list[str], str]:
    sentences = []
    current = ""
    for char in buf:
        current += char
        if char in SENTENCE_ENDINGS:
            sentences.append(current.strip())
            current = ""
    if not sentences and len(current) > MAX_BUFFER_BEFORE_FORCED_FLUSH:
        sentences.append(current.strip())
        current = ""
    return sentences, current


async def _generate_reply(session: CallSession, utterance_received_at: float) -> None:
    call_sid = session.call_sid
    lang = session.language or "en"
    history = memory.get_history(call_sid)
    scenario_hint = _current_scenario.get(call_sid)
    messages = build_messages(lang, history, scenario_hint)

    buffer = ""
    full_reply_parts: list[str] = []
    first_token_logged = False

    try:
        async for token in stream_completion(messages):
            if not first_token_logged:
                logger.info(f"[{call_sid}] time to first Groq token: {time.monotonic() - utterance_received_at:.2f}s")
                first_token_logged = True
            buffer += token
            complete_sentences, buffer = _extract_complete_sentences(buffer)
            for sentence in complete_sentences:
                if not sentence:
                    continue
                full_reply_parts.append(sentence)
                tts_start = time.monotonic()
                await _queue_speech(session, sentence, lang)
                logger.info(f"[{call_sid}] sentence TTS done in {time.monotonic() - tts_start:.2f}s: {sentence!r}")

        trailing = buffer.strip()
        if trailing:
            full_reply_parts.append(trailing)
            tts_start = time.monotonic()
            await _queue_speech(session, trailing, lang)
            logger.info(f"[{call_sid}] sentence TTS done in {time.monotonic() - tts_start:.2f}s: {trailing!r}")

    except asyncio.CancelledError:
        logger.info(f"[{call_sid}] reply generation cancelled (barge-in)")
        raise
    except Exception:
        logger.exception(f"[{call_sid}] Groq completion failed mid-stream")
    finally:
        full_reply = " ".join(full_reply_parts).strip()
        if full_reply:
            logger.info(f"[{call_sid}] assistant ({lang}, scenario={scenario_hint}): {full_reply!r}")
            memory.add_turn(call_sid, "assistant", full_reply)
        else:
            logger.warning(f"[{call_sid}] Groq returned an empty reply")


async def _queue_speech(session: CallSession, text: str, lang: str) -> None:
    try:
        audio_bytes = await _synthesize_speech(text, lang)
    except Exception:
        logger.exception(f"[{session.call_sid}] TTS synthesis failed for chunk: {text!r}")
        return

    queue = _audio_queues.get(session.call_sid)
    if queue is None:
        logger.warning(f"[{session.call_sid}] no audio queue found, dropping synthesized chunk")
        return
    queue.put_nowait(audio_bytes)


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
    call_sid = session.call_sid
    logger.info(f"[{call_sid}] call ended")

    reply_task = _reply_tasks.pop(call_sid, None)
    if reply_task and not reply_task.done():
        reply_task.cancel()
        try:
            await reply_task
        except asyncio.CancelledError:
            pass

    queue = _audio_queues.pop(call_sid, None)
    if queue:
        queue.put_nowait(None)
    sender_task = _sender_tasks.pop(call_sid, None)
    if sender_task:
        try:
            await sender_task
        except Exception:
            logger.exception(f"[{call_sid}] error in audio sender task during shutdown")

    task = _transcript_tasks.pop(call_sid, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    stream = _active_streams.pop(call_sid, None)
    if stream:
        try:
            await stream.close()
        except Exception:
            logger.exception(f"[{call_sid}] error closing Deepgram STT connection")

    _send_audio_callbacks.pop(call_sid, None)
    _current_scenario.pop(call_sid, None)
    _utterance_buffers.pop(call_sid, None)
    _last_fragment_time.pop(call_sid, None)
    memory.clear(call_sid)
