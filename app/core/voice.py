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
  any audio already queued but not yet sent. The queue is now also
  drained when a *finished* reply still has unsent audio queued —
  previously a caller interrupting after generation completed (but
  before playback finished) got overlapping speech.
- speech_final never fired reliably in testing; "UtteranceEnd" (a message
  type, not a distinct EventType) is the real end-of-utterance signal,
  with a timeout-based safety net in case it doesn't fire. The safety
  net now runs as its own watchdog task: the old inline check only ran
  when a *new* fragment arrived, which reset the timer it was checking —
  meaning it could never actually fire in the "caller spoke once, then
  UtteranceEnd never came" case it existed for.

Language auto-detection (dual-stream arbitration):
- Verified against Deepgram's current docs (July 2026): Nova-3's
  `language=multi` code-switching mode covers en/es/fr/de/hi/ru/pt/ja/
  it/nl ONLY — Arabic is NOT in the multi set. Nova-3 Arabic is a
  separate *monolingual* model (`ar` + 16 dialect codes). So genuine
  en/ar auto-detection cannot happen inside a single STT connection,
  and the previous code (open with `language="en"`, read a `languages`
  field back) could never detect Arabic: monolingual connections don't
  populate `languages` at all, so detection always defaulted to "en".
- Fix: each call opens TWO STT connections (en + ar) and feeds both the
  same audio. The first utterance is scored on both sides (transcript
  confidence); the winner becomes the call's language, the losing
  connection is closed, and the winning side's transcript is processed
  as the first turn. No audio is lost and the caller never announces a
  language. Decision inputs are logged for real-call verification.
- If ElevenLabs (Arabic TTS) isn't configured, the ar stream is not
  opened at all — an Arabic caller we can't answer in Arabic is worse
  served by half-detection — and a loud warning is logged at startup.

First-turn latency:
- A pre-synthesized bilingual greeting is queued immediately at call
  start, independent of Groq and even of STT connecting. It's cached
  in-process (pre-warmed at app startup) so no synthesis cost is paid
  per call. The greeting is recorded as an assistant turn so the LLM
  doesn't re-greet.
- Sentence audio is now queued as soon as its synthesis finishes (in
  original order, via an ordered relay task) instead of only after the
  full Groq stream completes — first-sentence audio can start playing
  while later sentences are still being generated/synthesized.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.core import language, memory
from app.models.schemas import CallSession
from app.prompts import kb_loader
from app.prompts.prompt_builder import build_messages
from app.services import deepgram_tts, elevenlabs_tts, groq_tts
from app.services.deepgram_stt import DeepgramSTTStream
from app.services.groq_llm import stream_completion
from config import settings

logger = logging.getLogger(__name__)

SendAudioFn = Callable[[bytes], Awaitable[None]]

# Per-call state, all keyed by call_sid.
_active_streams: dict[str, dict[str, DeepgramSTTStream]] = {}  # stt lang -> stream
_send_audio_callbacks: dict[str, SendAudioFn] = {}
_current_scenario: dict[str, str] = {}
_transcript_tasks: dict[str, list[asyncio.Task]] = {}
_utterance_buffers: dict[str, list[str]] = {}
_last_fragment_time: dict[str, float] = {}
_audio_queues: dict[str, asyncio.Queue] = {}
_sender_tasks: dict[str, asyncio.Task] = {}
_reply_tasks: dict[str, asyncio.Task] = {}
_watchdog_tasks: dict[str, asyncio.Task] = {}
_decision_timers: dict[str, asyncio.Task] = {}
_pending_detection: dict[str, dict] = {}
_stt_dead_notified: set[str] = set()

MIN_TRANSCRIPT_LENGTH = 2

FALLBACK_TEXT_BY_LANGUAGE = {
    "en": "Sorry, I didn't catch that. Can you say that again?",
    "ar": "عذراً، لم أفهم ذلك. هل يمكنك إعادة ما قلته؟",
}

STT_FAILURE_TEXT_BY_LANGUAGE = {
    "en": "I'm sorry, I'm having a technical problem hearing you. Please hang up and call again.",
    "ar": "عذراً، لدي مشكلة تقنية في سماعك. الرجاء إغلاق المكالمة والاتصال مرة أخرى.",
}

GREETING_BY_LANGUAGE = {
    "en": "Hi, I'm Najda. Tell me what happened.",
    "ar": "مرحباً، أنا نجدة. أخبرني ماذا حدث.",
}

# Spoken when a reply's text was generated but every sentence's TTS failed —
# the caller must hear *something* rather than a silent turn.
TTS_TROUBLE_TEXT_BY_LANGUAGE = {
    "en": "Sorry — I'm having audio trouble on my end. Please stay with me and say that again.",
    "ar": "عذراً، أواجه مشكلة صوتية من جهتي. ابقَ معي وأعد ما قلته من فضلك.",
}
# Spoken (in English, via Aura) when the call is Arabic but Arabic TTS is
# known-unusable — an English notice beats dead silence, and many callers
# can switch.
ARABIC_TTS_DOWN_TEXT_EN = (
    "I'm sorry — I'm having technical trouble speaking Arabic right now. "
    "If you can, please continue in English."
)

FRAGMENT_TIMEOUT_S = 3.0
WATCHDOG_POLL_S = 0.5
# When one STT side signals end-of-utterance but the other side hasn't
# produced anything yet, wait this long for the slower side's finals
# (different connections finalize at slightly different times) before
# deciding the call's language on one-sided evidence.
DETECTION_GRACE_S = 0.4
# Confidence gap required to lock the call's language permanently on one
# utterance. Real call testing showed a greeting like "هلا"/"Hello?" can
# score ~0.98 on BOTH models — a coin flip. Below this margin the decision
# is provisional: the reply goes out in the higher-scoring language, but
# both STT streams stay open and the next utterance re-arbitrates.
DECISION_MARGIN = 0.15
# Never HARD-lock on a short utterance, no matter the confidence gap.
# Greetings are near-homophones across this language pair ("يا أهلاً" was
# heard as "Yeah. Hi." at 0.98 by the English model in live testing, beating
# the correct Arabic read at 0.86 and wrongly locking the call to English) —
# and the first utterance is almost always a greeting. Substantive speech
# ("جرحت يدي بالسكين" / "I cut my hand") is longer and never ambiguous, so
# requiring length costs decisive callers nothing.
MIN_LOCK_TEXT_CHARS = 12
MAX_ARBITRATION_ROUNDS = 3

SENTENCE_ENDINGS = {".", "!", "?", "؟"}
MAX_BUFFER_BEFORE_FORCED_FLUSH = 200

_greeting_audio_cache: dict[str, bytes] = {}
_greeting_lock = asyncio.Lock()

# Runtime TTS health. Set when Arabic synthesis fails with a permanent-class
# error (401/402/403/404 — bad key, plan restrictions, missing voice). Once
# set, later calls run English-only instead of opening an ar STT stream whose
# replies could never be spoken (observed live: ElevenLabs 402
# "paid_plan_required" on a library voice left an Arabic caller in silence).
_tts_health = {"ar_dead": False}


def _arabic_enabled() -> bool:
    if _tts_health["ar_dead"]:
        return False
    provider = language.get_tts_provider("ar")
    if provider == "groq_orpheus":
        return bool(settings.groq_api_key)
    if provider == "elevenlabs":
        return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id_ar)
    return False


def arabic_tts_configured() -> bool:
    """Config-level check (ignores the runtime dead-flag) — used by startup
    logging in app/main.py."""
    provider = language.get_tts_provider("ar")
    if provider == "groq_orpheus":
        return bool(settings.groq_api_key)
    if provider == "elevenlabs":
        return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id_ar)
    return False


def _note_tts_failure(lang: str, exc: BaseException) -> bool:
    """Classifies a TTS failure. Returns True when it's permanent (retry is
    pointless). Marks Arabic TTS dead for the process on permanent failures
    so later calls degrade to English-only instead of silent-Arabic."""
    status = getattr(exc, "status_code", None)
    body_text = str(getattr(exc, "body", "") or "")
    permanent = status in (401, 402, 403, 404) or (
        # Groq returns 400 model_terms_required until the org admin accepts
        # a model's terms in the console — permanent until human action.
        status == 400 and "model_terms_required" in body_text
    )
    if permanent and lang == "ar" and not _tts_health["ar_dead"]:
        _tts_health["ar_dead"] = True
        detail = getattr(exc, "body", None) or exc
        provider = language.get_tts_provider("ar")
        hint = ""
        if provider == "elevenlabs":
            hint = (
                " If this is ElevenLabs 402 paid_plan_required: free-tier API keys cannot "
                "use *library* voices — pick a premade voice, upgrade the plan, or switch "
                "to the default Groq provider by removing TTS_PROVIDER_AR from .env."
            )
        elif provider == "groq_orpheus":
            hint = (
                " If the error mentions model_terms_required: the org admin must accept "
                "the model terms ONCE at https://console.groq.com/playground?model="
                "canopylabs%2Forpheus-arabic-saudi — then restart. Otherwise check Model "
                "Permissions/billing at console.groq.com, or set TTS_PROVIDER_AR=elevenlabs."
            )
        logger.error(
            f"Arabic TTS ({provider}) marked UNUSABLE for this process (HTTP {status}): "
            f"{detail}. Calls now run English-only.{hint}"
        )
    return permanent


async def prewarm_greeting_cache() -> None:
    """Synthesize and cache the fixed greeting lines once (called at app
    startup, and again lazily on the first call if startup prewarm hadn't
    finished). Failures are logged and non-fatal — the call flow degrades
    to whatever languages did cache."""
    async with _greeting_lock:
        for lang, text in GREETING_BY_LANGUAGE.items():
            if lang in _greeting_audio_cache:
                continue
            if lang == "ar" and not _arabic_enabled():
                continue
            try:
                _greeting_audio_cache[lang] = await _synthesize_speech(text, lang)
                logger.info(f"pre-synthesized '{lang}' greeting ({len(_greeting_audio_cache[lang])} bytes)")
            except Exception as exc:
                if _note_tts_failure(lang, exc):
                    # Permanent (already logged loudly with guidance) — don't
                    # stack-trace-spam on every later prewarm attempt.
                    logger.error(f"'{lang}' greeting prewarm failed permanently, giving up on it")
                else:
                    logger.exception(f"failed to pre-synthesize '{lang}' greeting")


async def handle_call_start(session: CallSession, send_audio: SendAudioFn) -> None:
    call_sid = session.call_sid
    logger.info(f"[{call_sid}] call started")

    # Audio-out plumbing first, so we can speak even if STT setup fails.
    _send_audio_callbacks[call_sid] = send_audio
    _utterance_buffers[call_sid] = []
    _last_fragment_time[call_sid] = 0.0
    _audio_queues[call_sid] = asyncio.Queue()
    _sender_tasks[call_sid] = asyncio.create_task(_audio_sender_loop(call_sid))

    if len(_sender_tasks) > 1:
        # Concurrent calls are supported, but during single-tester demos this
        # almost always means a stale browser dialer tab never hung up
        # (observed live: two calls hearing the same microphone, answering
        # over each other, and splitting the shared TTS rate limits).
        logger.warning(
            f"[{call_sid}] {len(_sender_tasks)} concurrent calls active — if this is one "
            f"tester, an old dialer tab is probably still connected; both calls will hear "
            f"the same mic and talk over each other"
        )

    # Fixed greeting, queued immediately — no Groq round trip, and it
    # plays while the STT connections below are still being opened.
    asyncio.create_task(_queue_greeting(session))

    stt_langs = ["en"]
    if _arabic_enabled():
        stt_langs.append("ar")
    elif _tts_health["ar_dead"]:
        logger.warning(
            f"[{call_sid}] Arabic detection disabled: Arabic TTS was marked unusable "
            f"earlier in this process (see the earlier error for the cause and fix) — running English-only"
        )
    else:
        logger.warning(
            f"[{call_sid}] Arabic detection disabled: Arabic TTS provider "
            f"'{language.get_tts_provider('ar')}' is not configured — running English-only"
        )

    streams: dict[str, DeepgramSTTStream] = {}
    for stt_lang in stt_langs:
        stream = await _connect_stt_with_retry(call_sid, stt_lang)
        if stream:
            streams[stt_lang] = stream

    if not streams:
        logger.error(f"[{call_sid}] no STT connection available — speaking failure notice instead of dead air")
        await _speak_with_retry(call_sid, STT_FAILURE_TEXT_BY_LANGUAGE["en"], "en")
        if _arabic_enabled():
            await _speak_with_retry(call_sid, STT_FAILURE_TEXT_BY_LANGUAGE["ar"], "ar")
        return

    if len(streams) == 1:
        # Only one recognizer — no arbitration possible; lock language now.
        only_lang = next(iter(streams))
        if len(stt_langs) > 1:
            logger.warning(f"[{call_sid}] only '{only_lang}' STT connected — locking call to '{only_lang}'")
        session.language = only_lang
    else:
        _pending_detection[call_sid] = {
            "sides": {L: {"frags": [], "conf": 0.0, "done": False} for L in streams},
            "last_activity": 0.0,
        }

    _active_streams[call_sid] = streams

    consumer_tasks = []
    for stt_lang, stream in streams.items():
        task = asyncio.create_task(_consume_transcripts(session, stream, stt_lang))
        task.add_done_callback(lambda t, L=stt_lang: _log_task_exception(call_sid, t, L))
        consumer_tasks.append(task)
    _transcript_tasks[call_sid] = consumer_tasks

    _watchdog_tasks[call_sid] = asyncio.create_task(_utterance_watchdog(session))


def _stt_wire_language(logical_lang: str) -> str:
    """Maps a logical language ("en"/"ar") to the Deepgram language code to
    open the connection with. Arabic can be biased to a specific dialect
    (ar-EG, ar-SA, ...) via STT_LANGUAGE_AR."""
    if logical_lang == "ar":
        code = (settings.stt_language_ar or "ar").strip()
        if code.lower().startswith("ar"):
            return code
        logger.warning(f"invalid STT_LANGUAGE_AR '{code}' (must be an ar* code) — using 'ar'")
    return logical_lang


async def _connect_stt_with_retry(call_sid: str, stt_lang: str) -> DeepgramSTTStream | None:
    wire_language = _stt_wire_language(stt_lang)
    for attempt in (1, 2):
        stream = DeepgramSTTStream(language=wire_language)
        try:
            await stream.connect()
            return stream
        except Exception:
            logger.exception(
                f"[{call_sid}] failed to open Deepgram STT connection for "
                f"'{stt_lang}' (code '{wire_language}', attempt {attempt}/2)"
            )
    return None


def _try_queue_greeting_part(session: CallSession, lang: str, spoken_parts: list[str]) -> None:
    call_sid = session.call_sid
    # If language is already known (single-stream call, or the caller
    # spoke before the greeting got queued), only greet in that language.
    if session.language and session.language != lang:
        return
    audio = _greeting_audio_cache.get(lang)
    if not audio:
        return
    if _reply_tasks.get(call_sid) is not None:
        return  # caller already spoke; a real reply is underway — stale greeting would overlap
    queue = _audio_queues.get(call_sid)
    if queue is None:
        return  # call already ended
    queue.put_nowait(audio)
    spoken_parts.append(GREETING_BY_LANGUAGE[lang])


async def _queue_greeting(session: CallSession) -> None:
    """Queues the fixed greeting. Whatever is already cached goes out
    IMMEDIATELY; only missing languages wait on prewarm. (Observed live:
    awaiting prewarm unconditionally made the already-cached English
    greeting queue behind a multi-second ElevenLabs failure.)"""
    call_sid = session.call_sid
    spoken_parts: list[str] = []
    missing = [
        lang for lang in ("en", "ar")
        if lang not in _greeting_audio_cache and (lang != "ar" or _arabic_enabled())
    ]

    for lang in ("en", "ar"):
        _try_queue_greeting_part(session, lang, spoken_parts)

    if missing:
        try:
            await prewarm_greeting_cache()
        except Exception:
            logger.exception(f"[{call_sid}] greeting prewarm failed")
        for lang in missing:
            _try_queue_greeting_part(session, lang, spoken_parts)

    if spoken_parts:
        # Recorded so the LLM knows it already greeted and asked.
        memory.add_turn(call_sid, "assistant", " / ".join(spoken_parts))


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


def _log_task_exception(call_sid: str, task: asyncio.Task, stt_lang: str = "") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        label = f" ('{stt_lang}')" if stt_lang else ""
        logger.error(f"[{call_sid}] transcript-consumer task{label} crashed", exc_info=exc)


def _log_reply_task_exception(call_sid: str, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"[{call_sid}] reply-generation task crashed", exc_info=exc)


async def _consume_transcripts(session: CallSession, stream: DeepgramSTTStream, stt_lang: str) -> None:
    call_sid = session.call_sid

    async for transcript in stream.receive_transcripts():
        # While _pending_detection exists the call is still arbitrating —
        # either undecided, or decided only provisionally (near-tie) — so
        # every stream keeps feeding the arbiter.
        if call_sid in _pending_detection:
            await _handle_pre_decision_transcript(session, stt_lang, transcript)
            continue

        # A stream that lost a *locked* arbitration stops being consumed.
        # (Its connection is also closed by _decide_language, which ends
        # this generator via the close sentinel — this check is the fast path.)
        if session.language is not None and stt_lang != session.language:
            break

        if transcript.get("flush"):
            await _flush_utterance(session)
            continue

        if not transcript.get("is_final"):
            continue

        fragment = transcript["text"].strip()
        if fragment:
            _utterance_buffers.setdefault(call_sid, []).append(fragment)
            _last_fragment_time[call_sid] = time.monotonic()

            # Language stays sticky after arbitration. Monolingual Nova-3
            # connections never populate the `languages` field, so this
            # only fires if Deepgram explicitly reports one (e.g. if the
            # en side is ever switched to `multi` mode in the future).
            reported = transcript.get("language")
            if reported:
                detected = language.detect_language(reported)
                if detected != session.language:
                    logger.info(f"[{call_sid}] Deepgram reported language '{reported}' → switching to '{detected}'")
                    session.language = detected


async def _handle_pre_decision_transcript(session: CallSession, stt_lang: str, transcript: dict) -> None:
    call_sid = session.call_sid
    pending = _pending_detection.get(call_sid)
    if pending is None:
        return
    side = pending["sides"].get(stt_lang)
    if side is None:
        return

    if transcript.get("flush"):
        if not _pending_has_content(pending):
            return  # VAD fired but neither model produced words yet
        side["done"] = True
        other_sides = [s for L, s in pending["sides"].items() if L != stt_lang]
        if all(s["done"] or s["frags"] for s in other_sides):
            # Every other side has either also finished or has evidence —
            # decide now, no reason to wait.
            await _decide_language(session, reason=f"utterance end on '{stt_lang}'")
        else:
            # The other recognizer might just be finalizing a bit slower;
            # give it a short grace window before one-sided decision.
            _schedule_decision(session)
        return

    if not transcript.get("is_final"):
        return

    fragment = transcript["text"].strip()
    if not fragment:
        return
    side["frags"].append(fragment)
    side["conf"] = max(side["conf"], float(transcript.get("confidence") or 0.0))
    pending["last_activity"] = time.monotonic()


def _pending_has_content(pending: dict) -> bool:
    return any(side["frags"] for side in pending["sides"].values())


def _schedule_decision(session: CallSession) -> None:
    call_sid = session.call_sid
    if call_sid in _decision_timers:
        return

    async def _decide_later() -> None:
        try:
            await asyncio.sleep(DETECTION_GRACE_S)
            await _decide_language(session, reason="grace period elapsed")
        except asyncio.CancelledError:
            pass

    _decision_timers[call_sid] = asyncio.create_task(_decide_later())


async def _decide_language(session: CallSession, reason: str = "") -> None:
    call_sid = session.call_sid
    pending = _pending_detection.pop(call_sid, None)
    if pending is None:
        return  # already decided (pop makes concurrent callers a no-op)

    timer = _decision_timers.pop(call_sid, None)
    if timer and timer is not asyncio.current_task():
        timer.cancel()

    scores: dict[str, float] = {}
    texts: dict[str, str] = {}
    for L, side in pending["sides"].items():
        text = " ".join(side["frags"]).strip()
        texts[L] = text
        scores[L] = side["conf"] if text else 0.0

    rounds = pending.get("round", 1)
    logger.info(
        f"[{call_sid}] language decision ({reason}, round {rounds}): "
        + "; ".join(f"{L}: conf={scores[L]:.3f} text={texts[L]!r}" for L in sorted(scores))
    )

    # Higher confidence wins; English wins exact ties (it's the default).
    winner = max(scores, key=lambda L: (scores[L], L == "en"))
    if not texts[winner]:
        _pending_detection[call_sid] = pending  # no usable speech yet — stay undecided
        return

    streams = _active_streams.get(call_sid, {})
    gap = abs(scores.get("en", 0.0) - scores.get("ar", 0.0))
    decisive = gap >= DECISION_MARGIN and len(texts[winner]) >= MIN_LOCK_TEXT_CHARS
    lock = decisive or rounds >= MAX_ARBITRATION_ROUNDS or len(streams) < 2

    # Never lock into a language we cannot speak: if Arabic TTS is known
    # unusable, an Arabic lock would produce silent replies.
    if winner == "ar" and _tts_health["ar_dead"] and "en" in streams:
        logger.error(f"[{call_sid}] caller appears to speak Arabic but Arabic TTS is unusable — locking 'en'")
        winner = "en"
        lock = True

    session.language = winner
    if lock:
        logger.info(f"[{call_sid}] language locked to '{winner}' (gap={gap:.3f})")
        for L in list(streams):
            if L != winner:
                losing = streams.pop(L)
                asyncio.create_task(_close_stream_quietly(call_sid, L, losing))
    else:
        # Near-tie or too-short utterance (e.g. "هلا" vs "Hello?"): answer
        # in the higher-scoring language now, but keep both streams open
        # and re-arbitrate on the caller's next utterance.
        logger.info(
            f"[{call_sid}] language provisionally '{winner}' "
            f"(gap={gap:.3f}, text_len={len(texts[winner])} < lock thresholds), "
            f"re-arbitrating on next utterance"
        )
        _pending_detection[call_sid] = {
            "sides": {L: {"frags": [], "conf": 0.0, "done": False} for L in streams},
            "last_activity": 0.0,
            "round": rounds + 1,
        }

    # The winning side's buffered fragments ARE the caller's first
    # utterance — process it now rather than making them repeat it.
    _utterance_buffers[call_sid] = list(pending["sides"][winner]["frags"])
    _last_fragment_time[call_sid] = time.monotonic()
    await _flush_utterance(session)


async def _close_stream_quietly(call_sid: str, stt_lang: str, stream: DeepgramSTTStream) -> None:
    try:
        await stream.close()
    except Exception:
        logger.exception(f"[{call_sid}] error closing '{stt_lang}' STT connection")


async def _utterance_watchdog(session: CallSession) -> None:
    """Timeout safety net for missing UtteranceEnd, in both phases.

    Runs as its own task: the previous inline implementation only checked
    elapsed time when a NEW fragment arrived — which had just reset the
    very timer being checked — so it could never fire in the actual
    failure case (caller spoke, then silence, no UtteranceEnd)."""
    call_sid = session.call_sid
    try:
        while True:
            await asyncio.sleep(WATCHDOG_POLL_S)
            now = time.monotonic()

            pending = _pending_detection.get(call_sid)
            if pending is not None:
                last = pending["last_activity"]
                if _pending_has_content(pending) and last and (now - last) > FRAGMENT_TIMEOUT_S:
                    logger.warning(f"[{call_sid}] no UtteranceEnd during language detection — deciding on timeout")
                    await _decide_language(session, reason="timeout")
                continue

            buffered = _utterance_buffers.get(call_sid)
            last = _last_fragment_time.get(call_sid, 0.0)
            if buffered and last and (now - last) > FRAGMENT_TIMEOUT_S:
                logger.warning(f"[{call_sid}] no UtteranceEnd received, flushing on timeout")
                await _flush_utterance(session)
    except asyncio.CancelledError:
        pass


async def _flush_utterance(session: CallSession) -> None:
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

    # Always drop queued-but-unsent audio on a new utterance — this covers
    # both an in-progress reply (above) and a reply whose generation already
    # finished but whose audio is still queued (previously kept playing over
    # the new turn), plus an unfinished greeting the caller talked over.
    await _drain_audio_queue(call_sid)

    lang = session.language or "en"

    matched = kb_loader.match_scenario(full_text, lang)
    if matched:
        if _current_scenario.get(call_sid) != matched:
            logger.info(f"[{call_sid}] scenario matched: {matched}")
        _current_scenario[call_sid] = matched

    logger.info(f"[{call_sid}] caller said ({lang}): {full_text!r}")
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


def _sentence_speakable(sentence: str, lang: str) -> bool:
    """A sentence is worth sending to TTS only if it (a) contains at least
    one letter or digit and (b) is majority-written in the call language's
    script — the Arabic voice can't speak an English meta-note and vice
    versa, and mismatched-script fragments are model leakage, not content."""
    if not any(ch.isalnum() for ch in sentence):
        return False
    letters = [ch for ch in sentence if ch.isalpha()]
    if not letters:
        return True  # digits only ("123") — speakable in either language
    arabic = sum(1 for ch in letters if "\u0600" <= ch <= "\u06ff")
    ratio = arabic / len(letters)
    return ratio >= 0.5 if lang == "ar" else ratio <= 0.5


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

    full_reply, spoke_any = await _stream_and_queue_reply(call_sid, messages, lang, utterance_received_at)

    if not full_reply and not spoke_any:
        logger.warning(f"[{call_sid}] Groq returned an empty reply, retrying once")
        full_reply, spoke_any = await _stream_and_queue_reply(call_sid, messages, lang, time.monotonic())

    if not full_reply and not spoke_any:
        logger.warning(f"[{call_sid}] Groq returned an empty reply twice — speaking fallback instead of silence")
        if lang == "ar" and _tts_health["ar_dead"]:
            # The Arabic canned fallback can't be spoken either — use the
            # English notice rather than a guaranteed-silent turn.
            fallback_text, fallback_lang = ARABIC_TTS_DOWN_TEXT_EN, "en"
        else:
            fallback_text = FALLBACK_TEXT_BY_LANGUAGE.get(lang, FALLBACK_TEXT_BY_LANGUAGE["en"])
            fallback_lang = lang
        await _speak_with_retry(call_sid, fallback_text, fallback_lang)
        memory.add_turn(call_sid, "assistant", fallback_text)
        return

    if full_reply and not spoke_any:
        # Text was generated but EVERY sentence's TTS failed — previously
        # this fell through silently (observed live with ElevenLabs 402:
        # Arabic replies were logged but the caller heard nothing). Speak a
        # trouble notice in whatever language can actually produce audio,
        # and record THAT in memory — not the reply the caller never heard.
        logger.error(f"[{call_sid}] reply text generated but no audio produced — speaking trouble notice")
        if lang == "ar" and _tts_health["ar_dead"]:
            notice_text, notice_lang = ARABIC_TTS_DOWN_TEXT_EN, "en"
        else:
            notice_text = TTS_TROUBLE_TEXT_BY_LANGUAGE.get(lang, TTS_TROUBLE_TEXT_BY_LANGUAGE["en"])
            notice_lang = lang
        await _speak_with_retry(call_sid, notice_text, notice_lang)
        memory.add_turn(call_sid, "assistant", notice_text)
        return

    if full_reply:
        logger.info(f"[{call_sid}] assistant ({lang}, scenario={scenario_hint}): {full_reply!r}")
        memory.add_turn(call_sid, "assistant", full_reply)


async def _stream_and_queue_reply(
    call_sid: str, messages: list[dict], lang: str, start_time: float,
) -> tuple[str, bool]:
    """Streams a Groq reply, firing each complete sentence's TTS as an
    asyncio.Task immediately (pipelined — sentence N+1's synthesis overlaps
    sentence N's). An ordered relay task queues each sentence's audio the
    moment it's ready (in original order), so the first sentence can start
    playing while Groq is still streaming later ones.

    Returns (reply_text, any_audio_queued). If Groq fails mid-stream after
    some sentences were already synthesized/spoken, those sentences are
    kept (not retried) — repeating half a reply is worse than a truncated
    one — and the returned text reflects what was actually produced."""
    buffer = ""
    full_reply_parts: list[str] = []
    tts_tasks: list[asyncio.Task] = []
    task_queue: asyncio.Queue = asyncio.Queue()
    relay = asyncio.create_task(_relay_tts_audio(call_sid, task_queue))
    first_token_logged = False
    groq_failed = False
    seen_sentences: set[str] = set()

    def _fire_sentence(sentence: str) -> None:
        if sentence in seen_sentences:
            # The model occasionally loops, emitting an entire reply twice
            # (observed live: 9 sentences duplicated -> 18 simultaneous TTS
            # requests -> provider 429s and dropped audio). Within ONE reply
            # an exact-duplicate sentence is looping, not intent — deliberate
            # escalation repeats happen across turns, and those hit the
            # audio cache anyway.
            logger.warning(f"[{call_sid}] dropping duplicated sentence within reply: {sentence!r}")
            return
        seen_sentences.add(sentence)
        if not _sentence_speakable(sentence, lang):
            # Observed live: the model occasionally leaks meta-asides into
            # its reply — an English "(Note: I ask one question at a time..."
            # spoken aloud by the Arabic voice, and a lone ")" fragment that
            # the TTS API rejected (400: needs at least one letter/digit).
            # Unspeakable fragments are dropped from BOTH audio and memory.
            logger.warning(f"[{call_sid}] dropping unspeakable/wrong-script sentence: {sentence!r}")
            return
        full_reply_parts.append(sentence)
        t = asyncio.create_task(_synthesize_speech_timed(call_sid, sentence, lang))
        tts_tasks.append(t)
        task_queue.put_nowait(t)

    try:
        async for token in stream_completion(messages):
            if not first_token_logged:
                logger.info(f"[{call_sid}] time to first Groq token: {time.monotonic() - start_time:.2f}s")
                first_token_logged = True
            buffer += token
            complete_sentences, buffer = _extract_complete_sentences(buffer)
            for sentence in complete_sentences:
                if sentence:
                    _fire_sentence(sentence)

        trailing = buffer.strip()
        if trailing:
            _fire_sentence(trailing)

    except asyncio.CancelledError:
        for t in tts_tasks:
            t.cancel()
        relay.cancel()
        try:
            await relay
        except (asyncio.CancelledError, Exception):
            pass
        raise
    except Exception:
        logger.exception(f"[{call_sid}] Groq completion failed mid-stream")
        groq_failed = True

    task_queue.put_nowait(None)
    try:
        queued_count = await relay
    except asyncio.CancelledError:
        # Barge-in while draining: kill the relay too, so it can't queue
        # any more audio after _flush_utterance drains the queue.
        for t in tts_tasks:
            t.cancel()
        relay.cancel()
        try:
            await relay
        except (asyncio.CancelledError, Exception):
            pass
        raise

    full_text = " ".join(full_reply_parts).strip()
    if groq_failed and queued_count == 0:
        return "", False
    if groq_failed:
        logger.warning(f"[{call_sid}] reply truncated by mid-stream Groq failure after {queued_count} sentence(s)")
    return full_text, queued_count > 0


async def _relay_tts_audio(call_sid: str, task_queue: asyncio.Queue) -> int:
    """Awaits TTS tasks in original sentence order and queues each
    sentence's audio as soon as it's ready. Returns how many sentences
    actually got queued."""
    queued = 0
    while True:
        tts_task = await task_queue.get()
        if tts_task is None:
            return queued
        try:
            audio_bytes = await tts_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{call_sid}] TTS synthesis failed for a queued sentence")
            continue
        if audio_bytes:
            queue = _audio_queues.get(call_sid)
            if queue is not None:
                queue.put_nowait(audio_bytes)
                queued += 1


async def _speak_with_retry(call_sid: str, text: str, lang: str) -> None:
    queue = _audio_queues.get(call_sid)
    for attempt in (1, 2):
        try:
            audio_bytes = await _synthesize_speech(text, lang)
            if queue is not None:
                queue.put_nowait(audio_bytes)
            return
        except Exception as exc:
            if _note_tts_failure(lang, exc):
                logger.error(f"[{call_sid}] fallback speech failed permanently for '{lang}' — not retrying")
                break
            logger.exception(f"[{call_sid}] fallback speech synthesis failed (attempt {attempt}/2)")
    logger.error(f"[{call_sid}] fallback speech synthesis failed — caller hears nothing this turn")


async def _synthesize_speech_timed(call_sid: str, text: str, lang: str) -> bytes | None:
    """One transparent retry on the main TTS path: Deepgram's TTS endpoint
    has been observed dropping connections outright mid-request
    (httpx.RemoteProtocolError) — previously that sentence was just lost."""
    started_at = time.monotonic()
    for attempt in (1, 2):
        try:
            audio_bytes = await _synthesize_speech(text, lang)
            logger.info(f"[{call_sid}] sentence TTS done in {time.monotonic() - started_at:.2f}s: {text!r}")
            return audio_bytes
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _note_tts_failure(lang, exc):
                # Permanent (auth/plan/voice errors) — retrying is pointless
                # and just doubles the API spam. Observed live: ElevenLabs
                # 402 paid_plan_required was retried for every sentence.
                logger.error(f"[{call_sid}] TTS failed permanently for '{lang}', skipping chunk: {text!r}")
                return None
            if attempt == 1:
                logger.warning(f"[{call_sid}] TTS synthesis failed for chunk (attempt 1/2), retrying: {text!r}")
            else:
                logger.exception(f"[{call_sid}] TTS synthesis failed for chunk (attempt 2/2), skipping: {text!r}")
    return None


# LRU cache of synthesized sentences. First-aid dialogue repeats fixed
# phrases constantly — the KB *scripts* the escalation phrase to be repeated
# verbatim ("اتصل بالإسعاف الآن. لا تتوقف عما تفعله." appeared 3+ times in one
# live call) — so repeats play instantly and spend zero TTS quota/latency.
# ~30KB per cached sentence; 128 entries ≈ 4MB worst case.
_tts_cache: OrderedDict[tuple[str | None, str, str], bytes] = OrderedDict()
TTS_CACHE_MAX_ENTRIES = 128


async def _synthesize_speech(text: str, lang: str) -> bytes:
    provider = language.get_tts_provider(lang)
    cache_key = (provider, lang, text)
    cached = _tts_cache.get(cache_key)
    if cached is not None:
        _tts_cache.move_to_end(cache_key)
        return cached

    if provider == "deepgram_aura":
        audio = await deepgram_tts.synthesize(text, language=lang)
    elif provider == "groq_orpheus":
        audio = await groq_tts.synthesize(text, language=lang)
    elif provider == "elevenlabs":
        audio = await elevenlabs_tts.synthesize(text, language=lang)
    else:
        raise ValueError(f"No TTS provider resolved for language '{lang}'")

    if audio:
        _tts_cache[cache_key] = audio
        _tts_cache.move_to_end(cache_key)
        while len(_tts_cache) > TTS_CACHE_MAX_ENTRIES:
            _tts_cache.popitem(last=False)
    return audio


async def handle_audio_chunk(session: CallSession, audio_bytes: bytes) -> None:
    call_sid = session.call_sid
    streams = _active_streams.get(call_sid)
    if not streams:
        return
    for stt_lang in list(streams):
        stream = streams.get(stt_lang)
        if stream is None:
            continue
        try:
            await stream.send_audio(audio_bytes)
        except Exception:
            # Don't log per 20ms chunk forever — drop the dead stream once.
            logger.exception(f"[{call_sid}] failed to forward audio to '{stt_lang}' STT stream — dropping it")
            streams.pop(stt_lang, None)
            asyncio.create_task(_close_stream_quietly(call_sid, stt_lang, stream))

    if not streams and call_sid not in _stt_dead_notified:
        _stt_dead_notified.add(call_sid)
        logger.error(f"[{call_sid}] all STT streams dead mid-call — telling caller instead of going silent")
        lang = session.language or "en"
        await _speak_with_retry(call_sid, STT_FAILURE_TEXT_BY_LANGUAGE.get(lang, STT_FAILURE_TEXT_BY_LANGUAGE["en"]), lang)


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

    watchdog = _watchdog_tasks.pop(call_sid, None)
    if watchdog:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

    timer = _decision_timers.pop(call_sid, None)
    if timer:
        timer.cancel()

    for task in _transcript_tasks.pop(call_sid, []):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    for stt_lang, stream in _active_streams.pop(call_sid, {}).items():
        try:
            await stream.close()
        except Exception:
            logger.exception(f"[{call_sid}] error closing '{stt_lang}' Deepgram STT connection")

    _pending_detection.pop(call_sid, None)
    _send_audio_callbacks.pop(call_sid, None)
    _current_scenario.pop(call_sid, None)
    _utterance_buffers.pop(call_sid, None)
    _last_fragment_time.pop(call_sid, None)
    _stt_dead_notified.discard(call_sid)
    memory.clear(call_sid)
