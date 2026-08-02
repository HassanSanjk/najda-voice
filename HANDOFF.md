# Najda Voice — Engineering Handoff (July 2026 review & fix cycle)

This document summarizes a full review-and-fix cycle performed on Najda Voice, verified across ~8 real WebRTC test calls. It is written so a person (or another AI assistant) with no prior context can continue the work. Read it together with the original engineering brief conventions: several things in this codebase look unusual but encode confirmed third-party behavior — do not "clean them up."

## 1. What this project is

Bilingual (English/Arabic) AI voice agent guiding callers through first-aid emergencies over a phone call.

Pipeline: Telnyx (telephony, TeXML + bidirectional media WebSocket) → FastAPI (`app/`) → Deepgram Nova-3 (STT, streaming) → Groq `openai/gpt-oss-20b` (LLM) → TTS: Deepgram Aura-2 (English) / **Groq-hosted Orpheus `canopylabs/orpheus-arabic-saudi` (Arabic — changed this cycle, see §4)** → mu-law 8 kHz audio back to caller.

Key modules: `app/core/voice.py` (turn orchestrator — most logic lives here), `app/services/deepgram_stt.py`, `deepgram_tts.py`, `groq_tts.py` (new), `elevenlabs_tts.py` (kept, no longer default), `groq_llm.py`, `app/core/language.py`, `app/core/memory.py` (summarizing history), `app/prompts/kb_loader.py` + `knowledge/KB_*.yaml` (8 scenario files), `app/routes/voice.py` (webhook + media WS), `app/routes/telnyx_token.py` (WebRTC test tokens), `config.py` (pydantic-settings, `.env`).

Runtime: Python 3.14 venv on Windows (dev), Docker (python:3.14-slim) for deployment. `audioop-lts` backport provides `audioop` on 3.13+.

## 2. The headline fix: Arabic auto-detection (dual-stream arbitration)

**The bug (confirmed):** every call opened one Deepgram connection with `language="en"`. Verified against Deepgram's current docs: Nova-3's `language=multi` code-switching covers ONLY en/es/fr/de/hi/ru/pt/ja/it/nl — **Arabic is not and cannot be in the multi set**. Nova-3 Arabic is a separate *monolingual* model (`ar` + 16 dialect codes). Monolingual connections never populate the `languages` field in results, so the old "read language back from Deepgram" detection received `None` every time and defaulted to "en". Arabic had structurally never worked at the STT layer. Additionally, `_flush_utterance` re-ran detection each turn and would have flipped any detected language straight back to "en" (latent flapping bug).

**The fix (in `app/core/voice.py`):** each call opens TWO STT connections (en + ar), both fed the same caller audio. The first utterance is scored on both sides using transcript confidence (`confidence` was added to the transcript dicts in `deepgram_stt.py`). Decision logic, tuned over three live iterations:

- Immediate decision when both sides have evidence at first UtteranceEnd; `DETECTION_GRACE_S = 0.4` wait when only one side has spoken; `FRAGMENT_TIMEOUT_S = 3.0` watchdog as backstop.
- `DECISION_MARGIN = 0.15`: below this confidence gap the decision is **provisional** — reply goes out in the higher-scoring language, both streams stay open, next utterance re-arbitrates (`MAX_ARBITRATION_ROUNDS = 3`, then best-effort lock).
- `MIN_LOCK_TEXT_CHARS = 12`: **never hard-lock on a short utterance regardless of gap.** Live incident: caller said «يا أهلاً», English model heard "Yeah. Hi." at 0.98 vs the correct Arabic read at 0.86 → wrongly locked English. Greetings are near-homophones across this pair and are almost always the first utterance; substantive sentences are longer and never ambiguous.
- On lock: `session.language` set (sticky thereafter), losing stream closed, the winning side's buffered transcript becomes the first user turn (no audio lost, caller never repeats).
- If Arabic would win but Arabic TTS is known-dead (§5), lock English instead — never lock a language we can't speak.
- Arabic STT dialect can be biased via `STT_LANGUAGE_AR` (default `ar`; e.g. `ar-EG`, `ar-SA`).

**Live-verified:** «مرحبا» → provisional → «كنت في المطبخ وبالخطأ...» → locked ar (gap 0.994). "Hello? Help me." (15 chars, gap 0.607) → locked en immediately round 1. Gulf dialect («طحت», «ابشر») transcribed correctly.

**Known limitation:** after final lock there is no mid-call language switching (single remaining stream). Deepgram's Voice Agent API remains rejected for the reasons in the original brief (Flux has no Arabic; re-verified this cycle: `flux-general-multi` = same 10 languages, no Arabic).

## 3. Reliability & latency fixes in the orchestrator (`app/core/voice.py`)

All of these were driven by confirmed live failures or the original brief's flags:

- **Dead timeout net → real watchdog task.** The old inline "~3s no fragments → flush" check only ran when a NEW fragment arrived — which reset the timer it was checking — so it could never fire. Replaced with a per-call watchdog task (`WATCHDOG_POLL_S = 0.5`) covering both the detection phase and normal turns. Observed firing correctly in production.
- **Barge-in extended.** New utterance now ALWAYS drains queued-but-unsent audio — previously a caller interrupting after generation finished (but before playback finished) got overlapping speech. Sentence in flight (~2-4 s) remains the interruption granularity.
- **Pre-synthesized bilingual greeting.** Fixed opening line (en+ar) cached in-process, pre-warmed at app startup (`prewarm_greeting_cache()` from `main.py` lifespan), queued instantly at call start — no Groq/TTS round trip. Recorded as an assistant turn so the LLM knows it greeted. Cached parts queue immediately; only missing languages wait on prewarm (a lock-contention bug once delayed the greeting ~3 s behind a failing ar prewarm).
- **Ordered TTS relay.** Sentence-level TTS tasks still fire immediately as Groq streams (the original pipelining is preserved), but audio is now queued as soon as each sentence finishes IN ORDER via a relay task, instead of only after the whole Groq stream ended. On mid-stream Groq failure after sentences were spoken, the spoken part is kept (no half-reply repeats); memory records only what the caller actually heard.
- **Main-path TTS retry** (1 transparent retry per sentence) — for Deepgram's observed random disconnects.
- **No-dead-silence hardening:** STT connect retried once per stream; zero streams → spoken failure notice; a stream dying mid-call is dropped once (no per-20 ms log spam), all-dead → spoken notice once. Reply-text-generated-but-zero-audio turns speak a trouble notice instead of silent success (observed live with the ElevenLabs 402).
- **Groq empty completions — root cause found.** `gpt-oss-20b` is a reasoning model defaulting to `reasoning_effort="medium"`; reasoning tokens counted against the old `max_completion_tokens=300`, so short filler inputs ("Wait.", "Oh my god.") could burn the whole budget before any content token. Fix in `groq_llm.py`: `reasoning_effort="low"`, cap 640, plus `if not chunk.choices: continue` guard (choices-less trailing chunks aborted replies). The retry + canned-fallback net was kept per the brief. Zero empty completions in all subsequent calls, including the exact trigger phrases.
- **Model quirk mitigations (live-observed):** within-reply exact-duplicate sentence suppression (model looped a 9-sentence reply twice → 18 parallel TTS calls → provider 429s); speakability filter dropping sentences with no letters/digits (a lone ")" caused TTS 400s) or majority-wrong-script (English "(Note: I ask one question at a time…" was spoken aloud by the Arabic voice). Dropped sentences are excluded from memory too.
- **Sentence audio LRU cache** (128 entries, ~4 MB max, all providers): first-aid dialogue repeats fixed phrases by design (KB-scripted escalation line appeared 3+ times in one call) — repeats now cost 0 quota and 0 latency (visible as "TTS done in 0.00s").
- **Concurrent-call warning:** logged when >1 call is active — during single-tester demos this almost always means a stale browser dialer tab (observed: two calls hearing the same mic, answering over each other). Concurrent calls ARE supported architecturally (all state keyed by call_sid, cleaned in `handle_call_end`); the practical limit is shared per-model rate limits.
- Quiet shutdown: caller-hangup races (`WebSocketDisconnect` and starlette's "close message has been sent" RuntimeError) log one info line instead of stack traces. `asyncio.get_running_loop()` in the frame pacer.

## 4. Arabic TTS: ElevenLabs → Groq Orpheus (user-sanctioned provider change)

**Why:** live 402 `paid_plan_required` — ElevenLabs free-tier API keys cannot use *library* voices; the Arabic caller heard nothing while replies were logged as spoken. User asked for a free alternative.

**Solution:** `app/services/groq_tts.py` (new) using Groq-hosted `canopylabs/orpheus-arabic-saudi` — Groq's supported replacement for the deprecated `playai-tts-arabic`, billed on the SAME `GROQ_API_KEY` as the LLM. Verified specifics:

- Orpheus supports ONLY `response_format="wav"` (the SDK's `mulaw` literal is for legacy playai models). The service converts WAV → PCM → 8 kHz mono mu-law locally via `audioop` (stdlib <3.13, `audioop-lts` backport on 3.13+ — already in requirements for exactly this). Conversion handles any rate/channels/width; unit-tested.
- 200-char input cap per request → word-boundary splitter.
- Six Saudi voices: abdullah/fahad/sultan (m), lulwa/noura/aisha (f). **User auditioned via `scripts/test_arabic_tts.py` and chose `abdullah`** (set in `.env`). Note: Saudi dialect only — an Egyptian caller hears a Saudi accent (cosmetic, accepted).
- One-time human step: the org admin must accept model terms once at console.groq.com (API returns 400 `model_terms_required` until then — classified as permanent, see §5).
- Provider is switchable: `TTS_PROVIDER_AR=groq` (default) or `elevenlabs` (path kept intact, incl. its no-default-voice RuntimeError). `app/core/language.py::get_tts_provider()` resolves dynamically.
- **Free-tier limits (measured live): 100 requests/day, 1200 tokens(≈chars)/min.** One sentence = one request. The reply pipeline's concurrent bursts tripped 429 storms (6-15 s sentence delays from retry-after backoffs) → Orpheus requests are serialized via semaphore, size from `GROQ_TTS_CONCURRENCY` (default 1; set 3 after upgrading to Groq Developer tier). Serialization is inaudible: playback runs ~3 s/sentence while synthesis takes ~0.5-1 s. Aura got a fixed semaphore(3) for the same reason.
- `scripts/test_arabic_tts.py` is a 4-stage diagnostic (env → API reachability + model visibility via models.list → LLM ping → all six voices synthesized to `voice_samples/*.wav` + the exact mulaw conversion), printing full exception cause-chains. Works from any cwd (pins to project root before loading `.env`).

## 5. TTS health model & degradation ladder

`_tts_health["ar_dead"]` (process-level) is set when Arabic synthesis fails with a **permanent-class** error: HTTP 401/402/403/404, or Groq's 400 `model_terms_required`. Effects: no retry on permanent errors (they were being retried per sentence), one loud actionable log (with the exact console URL / fix per provider), later calls run English-only (ar STT stream not opened — never detect a language we can't speak), undecided arbitration never locks ar, and if a locked-ar call generates text but zero audio, an ENGLISH notice is spoken ("technical trouble speaking Arabic… continue in English") — memory records the notice, not the unheard reply. The same en-notice applies on the empty-reply fallback path. 429s are deliberately NOT permanent (minute-window recovery; SDK honors retry-after). Arabic disabled-at-startup logs the true reason (unconfigured vs marked-dead).

## 6. KB / scenario matching fixes (`app/prompts/kb_loader.py`)

- **Dangerous misroute fixed (live incident):** a car-crash victim («رجل دهسته سيارة، مغمى عليه ويتنفس») matched KB_Choking → Heimlich guidance for a trauma patient. Two stacked causes: the over-generic keyword «تنفس»/"breathing" (present in almost every emergency description) shared by choking and cpr, and **OS-dependent first-match order** — Windows sorts paths case-insensitively so KB_Choking beat KB_CPR on the dev machine while Linux gave the opposite. Fixes: breathing-family keywords removed from both lists; «مغمى»/«أغمي» (colloquial "passed out" — what real callers say) added to cpr; "cannot breathe" phrase kept for choking (contraction normalization makes "can't breathe" match it too); explicit case-insensitive sort so ordering is identical on every OS.
- **Arabic orthographic normalization** applied to both transcript and keywords: strip harakat/tatweel (U+064B–U+0652, U+0670, U+0640), unify hamza-seated alefs → ا, ى → ي, ة → ه. Fixes the substring-matching fragility the module's own LIMITATION note flagged.
- Remaining known limits: matching is still substring-based (an LLM-side scenario classifier is the ~1-hour structural upgrade if more misroutes appear); scenario is sticky once matched; **there is no KB for trauma/road accidents, stroke, seizures, poisoning, drowning** — uncovered emergencies fall to the generic router, which safely defers to real emergency services but is shallow. Authoring more `knowledge/KB_*.yaml` files (schema is proven) is a content task for the owner.

## 7. Other fixes

- `/telnyx-token` router existed but was **never mounted** in `main.py` (WebRTC test auth 404'd), and mounting exposed a missing `telnyx_telephony_credential_id` field in `config.py` (would have 500'd). Both fixed; `.env.example` updated.
- `main.py` lifespan: greeting prewarm task; accurate Arabic-provider status logging.
- `.env` additions this cycle: `GROQ_TTS_VOICE_AR=abdullah` (set), and available knobs `TTS_PROVIDER_AR`, `GROQ_TTS_CONCURRENCY`, `STT_LANGUAGE_AR` (documented in `.env.example`).

## 8. Confirmed-facts ledger (original brief items — status)

Untouched and still true: all Telnyx protocol quirks (bidirectional attrs, top-level `stream_id`, outbound envelope without stream id, self-echo filter on `track=="outbound"`), Deepgram `EventType` having only 4 members / UtteranceEnd as a MESSAGE type / `utterance_end_ms` requirement, Aura mulaw/8000/none (and Aura-has-no-Arabic), `openai/gpt-oss-20b` (Llama lineup remains deprecated), Groq empty-reply retry+fallback net, per-sentence pipelined TTS, sender-task/queue architecture, barge-in, Voice Agent API rejection (re-verified), audioop-lts note.
Modified WITH justification: timeout net rewritten (was provably dead code — behavior preserved, now functional); Groq params additive (`reasoning_effort`, cap 640); **Arabic TTS default provider changed from ElevenLabs to Groq Orpheus at the user's request after the confirmed 402** — ElevenLabs remains selectable and its deliberate no-default-voice guard is intact.

## 9. Verified live vs. still open

Verified across real calls: dual-stream arbitration (all paths: immediate lock, provisional→lock, timeout decision, homophone protection), Gulf-dialect Arabic STT, Orpheus Arabic TTS end-to-end, full KB triage flows in both languages (bleeding severe/embedded-object, fractures closed-branch), escalation repetition, barge-in, greeting-from-cache, watchdog flush, cache hits, empty-completion fix (exact trigger phrases retested), degradation ladder (402 day), concurrent calls (accidental two-tab test), clean hangups.

Open items (deliberate, not bugs):
1. **Groq Developer tier** (~$0.10/long Arabic call, no subscription, spend-limit settable; free tier = 100 req/day + 1200 TPM which stalls multi-sentence replies) → then set `GROQ_TTS_CONCURRENCY=3`. Paid tier is NOT smarter — identical models, only limits/billing change.
2. **EC2 deploy** (us-east-1, t3.micro/t2.micro, Docker path fully prepared incl. compose forcing APP_ENV=production, DuckDNS repoint keeps webhook URL unchanged; add 1 GB swap before first build). Realistic latency gain ~0.4-0.8 s/turn (Deepgram proximity; Groq already serves from bom/syd per response headers). The floor that remains everywhere: ~1 s `utterance_end_ms` + 0.5-2 s first-sentence synthesis.
3. Demo hygiene: ONE dialer tab (warning now logs if not), headset (acoustic echo would make the agent hear itself — the protocol echo is filtered, room echo is not).
4. Optional prompt tuning (owner's call, never applied): a line in `system_ar.txt`/`system_en.txt` banning parenthetical asides (everything is spoken), and one capping reply length (saves quota + latency; model currently over-talks its "short sentences" instruction).
5. KB breadth (see §6). Mid-call language switching (see §2). Pause-based mid-sentence flushes (~1 s endpointing tradeoff — callers recover naturally; tunable via `utterance_end_ms` at connect if ever desired).

## 10. Testing conventions used this cycle

Offline harnesses (stubbed SDKs, fake STT streams scripted per language, fake Groq/TTS) exercised: arbitration scenarios (decisive/near-tie/one-sided/timeout/homophone regression replaying the real «يا أهلاً» incident), 402 degradation, barge-in drain, fallback paths, dedup, speakability, KB matching (incl. reversed-file-order to simulate Windows), WAV→mulaw conversion against synthetic audio, and the project's own `tests/test_local.py` KB/prompt tests (updated expectation: "cannot breathe" phrase). ~40+ checks, all passing at handoff. Cheap live testing = Telnyx WebRTC browser calls (same webhook/WS as PSTN). Decision inputs, per-sentence TTS timings, and TTFT are logged per call for exactly this kind of verification.

