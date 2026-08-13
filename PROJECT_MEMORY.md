# Najda Voice — Full Project Memory & Handoff

**Quickstart — read this first (for a fresh AI session):**
- **What:** bilingual (English/Arabic) first-aid AI voice agent (phone → STT → LLM → TTS → phone). Solo demo/portfolio project by Hassan (Year 2 CS, APU).
- **Git:** repo `https://github.com/HassanSanjk/najda-voice.git`, branch `main`, HEAD `2b2a0c3` ("switch English TTS to Aura-2 Apollo", pushed). Working tree also contains **untracked** files: `HANDOFF.md` (July-cycle engineering handoff, related but different doc), `tests/check_telnyx.py`, and this file. `HANDOFF.md` and `tests/check_telnyx.py` are not yet committed.
- **The one active task:** §5 — naturalness/anti-repetition fix is **implemented and live-tested** (code + docs this cycle). The Aug 2 live repeat-question test confirmed the naturalness behavior working but surfaced **two new bugs** — an Arabic scenario misroute (دم⊂صدمة substring match) and a verbatim triage-question repeat on an unclassifiable answer — both **fixed in §4.29**. Remaining step: a **follow-up live re-test** of the two fixes before §6 gets updated.
- **Files a new session must read first:** this file → `README.md` (861-line architecture + decision-log reference, commit `b6dcc6a`) → `config.py` → `app/prompts/system_en.txt` → `app/prompts/kb_loader.py` → `app/core/voice.py`.
- **Golden rule:** anything in §4 that looks unusual, brittle, or over-engineered is very likely a deliberate fix for a confirmed live failure — check the ledger before "cleaning it up."

**Purpose of this document:** this is a complete context transfer so a fresh Claude Sonnet 5 session can continue this project with zero loss of history. It captures not just *what* the system does, but *why* every non-obvious decision was made and *which bugs were already found and fixed* — so the next session doesn't re-litigate settled decisions, re-discover already-fixed bugs, or "helpfully" revert something that looks unusual but is actually a deliberate correction for a confirmed live failure.

**Read this entire document before touching any code or making recommendations.**

---

## 1. What this project is

**Najda Voice** ("نجدة" — Arabic for "help"/"rescue") is a bilingual (English/Arabic) AI voice agent that guides callers through first-aid emergencies over a real phone call.

- Built by **Hassan**, a Year 2 CS student at APU (Asia Pacific University), as a solo portfolio/resume project.
- **Origin:** evolved from "First Aid Assistant," a group chatbot built in Botpress for an Introduction to AI course, shared with teammates Yahya, Emad, Mohammed Eissa, and Mohammed Al-Kaf. Hassan extracted the knowledge base and rebuilt the whole thing individually as a deployed, production-shaped voice product.
- **Purpose:** demo/portfolio only — not deployed for real users, no medical liability. Chosen over a generic "Arabic customer service agent" concept specifically because it's more emotionally compelling, memorable in interviews, and the knowledge base already existed from the group project.
- **Scope discipline:** this is explicitly a demo project on a tight solo timeline (originally a 12-day plan). Cost target was $0 out of pocket using free tiers/credits.

---

## 2. Final tech stack (with rationale — do not re-litigate these without new information)

| Layer | Choice | Why |
|---|---|---|
| Telephony | **Telnyx**, using a **TeXML Application** (not "Voice API Application"/Call Control) | Twilio's trial account blocks calls from unverified caller IDs, and Malaysian numbers cannot be verified at all — Hassan literally could not call his own Twilio number without adding real payment. Telnyx has no such restriction (~$2 total setup cost). TeXML specifically (not the JSON-webhook Voice API/Call Control model) was required to match the Twilio-style XML+webhook flow already built. |
| STT | **Deepgram Nova-3**, dual-stream (English + Arabic connections opened simultaneously per call) | Nova-3 has genuine Arabic dialect support (17 dialect codes incl. Sudanese `ar-SD`), but its `language="multi"` code-switching mode does **not** include Arabic (only en/es/fr/de/hi/ru/pt/ja/it/nl). Arabic is a separate **monolingual** model, and monolingual connections never populate the `languages` result field — so genuine auto-detection requires running both language models at once and arbitrating between them (see §5, item 9). |
| LLM | **Groq**, model `openai/gpt-oss-20b` | Groq deprecated its entire Llama chat lineup (June 2026). `gpt-oss-20b` is the fastest hosted model on Groq (~963 tok/s, ~0.73s TTFT) — chosen deliberately over `qwen/qwen3.6-27b` (better multilingual benchmarks but ~8x the cost and meaningfully slower for the 30–50 token replies this project actually generates). Matches the project's "latency above all else" priority. |
| TTS (English) | **Groq Orpheus English** (`canopylabs/orpheus-v1-english`) — *originally Deepgram Aura-2, changed Aug 2026* | Consolidated English onto the same Orpheus provider as Arabic (one SDK, one client, one semaphore, one WAV→mu-law path). Rationale: Deepgram English showed a ~1.3-1.6s fixed TTS floor vs ~0.4-0.9s for Groq Arabic, and two providers meant two leak/throttle surfaces and an audio-format asymmetry. `deepgram_tts.py` is **kept for rollback** (`language.py::TTS_PROVIDER_BY_LANGUAGE["en"]` is the switch). Voice via `GROQ_TTS_VOICE_EN` (default `austin`). Deepgram remains for STT only. |
| TTS (Arabic) | **Groq-hosted Orpheus** (`canopylabs/orpheus-arabic-saudi`) — *originally ElevenLabs, changed mid-project* | Deepgram Aura has **zero** Arabic support at all (confirmed: Aura-2 covers en/es/nl/fr/de/it/ja only — permanent platform limitation, not a bug). ElevenLabs (`eleven_flash_v2_5`) was the original choice, but a live 402 `paid_plan_required` error was hit during real Arabic testing — ElevenLabs free-tier keys can't use library voices, so Arabic callers heard nothing while the system logged replies as "spoken" (silent failure). Switched to Groq Orpheus at Hassan's request for a free/cheap alternative, billed on the same `GROQ_API_KEY` as the LLM. ElevenLabs path is kept in the codebase and selectable via `TTS_PROVIDER_AR=elevenlabs`, just no longer the default. |
| Backend | **FastAPI** (not Flask) | The whole pipeline is a chain of async I/O calls (telephony → STT → LLM → TTS). Hassan already knew Flask conceptually, so the transfer cost was low (~half a day for the async mental model). |
| Hosting | **Oracle Cloud** (me-riyadh-1, Ampere ARM), **Docker** (`python:3.14-slim`) behind a **Caddy HTTPS reverse proxy** | **Deployed live (Aug 2026).** `docker-compose.yml` binds `127.0.0.1:8000`; Caddy terminates TLS and forwards to it. Reserved Public IP — no cold-start/start-stop juggling needed (the old EC2 t2/t3.micro plan, §7.1, is superseded). |
| Dynamic DNS | **DuckDNS (name only)** | The `najda-voice.duckdns.org` name is kept as the stable public hostname/webhook origin so the Telnyx webhook URL never changes; with a Reserved Public IP the dynamic-update script is redundant. |
| Safety net | **CloudWatch auto-stop + AWS Budgets alert** | Planned (Day 11 of the original AWS build plan) but never confirmed implemented, and now moot on Oracle Cloud — verify Oracle's compute auto-stop + billing alert equivalents instead. See §7.2. |
| Cheap testing | **Telnyx WebRTC browser calling** (SIP Connection, credentials auth, via webrtc.telnyx.com) | Real calls from Malaysia to the Telnyx US number cost real international carrier money. WebRTC calling is ~$0.007/min billed to the Telnyx account instead, and hits the *exact same* `/voice` webhook and `/ws/media` WebSocket as a real PSTN call — zero code changes needed to test this way. |

**Explicitly rejected:** Deepgram's unified Voice Agent API (single WebSocket doing STT+LLM+TTS+turn-taking natively) was seriously evaluated as a full rearchitecture and **rejected**, for two confirmed (not hypothetical) reasons: (1) its Flux turn-detection/barge-in engine does not support Arabic at all, and (2) multiple developers have *active, unresolved* integration bugs trying to use ElevenLabs as a custom "speak" provider inside that API, visible in Deepgram's own GitHub discussions. This was **re-verified in a later session** and both blockers were confirmed still current, not stale. One idea *was* borrowed from a tutorial repo that inspired this investigation: the "clear queued audio on interruption" barge-in pattern — though implemented as our own queue-draining, since Telnyx's WebSocket protocol was never confirmed to support an equivalent client-sendable "clear" event the way Twilio's docs describe.

---

## 3. Architecture / file structure (as of the last major fix cycle)

```
najda-voice/
├── README.md                       # 861-line architecture + Engineering Decisions & Corrections reference (commit b6dcc6a)
├── .env / .env.example             # env inventory in §10
├── run.py                          # uvicorn entrypoint
├── config.py                       # pydantic-settings, validates env vars
│
├── app/
│   ├── main.py                     # FastAPI app factory + lifespan (incl. greeting prewarm)
│   ├── routes/
│   │   ├── voice.py                # POST /voice webhook + WS /ws/media
│   │   └── telnyx_token.py         # WebRTC test-call token auth (was built but never mounted — fixed)
│   ├── core/
│   │   ├── voice.py                # Turn orchestrator — MOST of the logic lives here
│   │   ├── memory.py                # Per-call history + background summarization
│   │   └── language.py              # Language/TTS-provider resolution
│   ├── services/
│   │   ├── deepgram_stt.py          # Streaming STT (dual-stream arbitration lives partly here, partly in core/voice.py)
│   │   ├── deepgram_tts.py          # English TTS (rollback-only — kept since Aug 2026 consolidation)
│   │   ├── groq_tts.py              # Arabic + English TTS via Groq Orpheus (single provider both langs)
│   │   ├── elevenlabs_tts.py        # Kept, selectable via TTS_PROVIDER_AR, no longer default
│   │   └── groq_llm.py              # Streaming LLM completion
│   ├── prompts/
│   │   ├── system_en.txt / system_ar.txt   # Persona (anti-repetition + natural-acknowledgment instructions — see §4.27)
│   │   ├── prompt_builder.py                # Assembles the final LLM prompt
│   │   └── kb_loader.py                     # YAML KB parsing + scenario matching + Arabic normalization
│   └── models/schemas.py
│
├── knowledge/                      # 8 scenarios — NO coverage yet for trauma/stroke/seizure/poisoning/drowning
│   ├── KB_Bleeding.yaml
│   ├── KB_Burns.yaml
│   ├── KB_Choking.yaml
│   ├── KB_CPR.yaml
│   ├── KB_ElectricShock.yaml
│   ├── KB_Fractures.yaml
│   ├── KB_SnakeBites.yaml
│   └── KB_AllergicReactions.yaml
│
├── scripts/
│   ├── update_duckdns.sh
│   ├── start.sh
│   ├── health_check.sh
│   └── test_arabic_tts.py          # 4-stage diagnostic: env → API reachability → LLM ping → synthesize all 6 voices
│
├── tests/test_local.py             # Offline harness: KB matching, prompt assembly (python tests/test_local.py)
├── tests/test_contraction.py       # Contraction-normalization quick test
├── tests/check_telnyx.py           # Telnyx connectivity/credential diagnostic (untracked)
├── Dockerfile / docker-compose.yml / .dockerignore
└── logs/
```

---

## 4. The full chronological bug/decision log

This is the most important section. Each entry below represents something **confirmed** through real testing (live phone calls, WebRTC test calls, or direct SDK introspection) — not guessed or assumed. If you see code that looks unusual, brittle, or "over-engineered," check this list before "cleaning it up."

1. **Python 3.14.4 dev environment.** `audioop` was removed from the stdlib in Python 3.13+ (PEP 594) — `audioop-lts` backport is required in `requirements.txt` for any mu-law/PCM conversion code (used for Groq Orpheus WAV→mulaw conversion, see item 14).

2. **Twilio abandoned for Telnyx.** Twilio trial accounts only accept calls from *verified* caller IDs, and Malaysian numbers cannot be verified at all — Hassan could not call his own Twilio number without adding real payment to remove the restriction. Telnyx has no equivalent restriction (~$2 total cost: $1 number + $1 first month).

3. **Telnyx "Voice API Application" vs "TeXML Application" confusion.** These are two entirely different integration models on Telnyx, easy to conflate. Voice API/Call Control sends JSON call-event webhooks and expects you to issue separate authenticated REST commands back (no XML response involved at all — this explained an early "empty form data" mystery, since Telnyx was correctly sending JSON to a number assigned to the wrong connection type, and our code was looking for form-encoded data). TeXML Application is the Twilio-compatible XML+webhook flow this project actually needs. **The number must be assigned to a TeXML Application, not a Voice API Application.**

4. **"Telephony Credential" in Telnyx is only for WebRTC/browser calling auth** (generates a JWT for the browser SDK) — it is **not** needed for the phone-call/TeXML webhook path. This was a real point of confusion early on; don't chase setting this up unless specifically doing WebRTC test-calling auth.

5. **Telnyx's WebSocket media-streaming protocol diverges from Twilio's documented conventions in several confirmed, non-obvious ways** (discovered via live diagnostic logging of real call payloads, one at a time, across many debugging rounds):
   - Inbound `start` event: **`stream_id` is a top-level sibling of `start`, not nested inside it** (unlike Twilio's `streamSid`, which Twilio nests). `call_control_id` (the call identifier), however, **is** nested inside `start`.
   - Inbound `media` events also carry `stream_id` top-level, not nested.
   - **Outbound envelope needs no stream identifier at all**: `{"event": "media", "media": {"payload": ..., "track": "outbound"}}` — Telnyx caps bidirectional streaming at exactly one stream per call, so there's no ambiguity to resolve with an ID.
   - **Bidirectional streaming (sending audio back) requires explicit opt-in attributes** on the TeXML `<Stream>` tag: `bidirectionalMode="rtp"`, `bidirectionalCodec="PCMU"`, `track="both_tracks"`. Without these, the stream silently defaults to receive-only — our sends completed with **zero errors**, but the caller heard nothing at all. This was one of the hardest bugs in the whole project to isolate, because everything *looked* like it was working from our own logs.
   - **Enabling bidirectional + `track="both_tracks"` causes Telnyx to echo our own outbound TTS audio back to us**, tagged `track: "outbound"`. This **must** be filtered out before forwarding to Deepgram STT — without the filter, Najda would transcribe and respond to her own voice, producing fragmented, self-echoing conversations (a real, confirmed live bug, not hypothetical).
   - `/voice` webhook form fields (`CallSid`, `From`, `To`, `CallStatus`) **are** genuinely Twilio-compatible in naming — confirmed via diagnostic logging, no special handling needed there.

6. **Deepgram SDK facts, confirmed via direct introspection of the installed package** (doc snippets found online were inconsistent across SDK versions, so these were verified by actually inspecting the installed `deepgram-sdk` v7.x classes, not trusted from docs alone):
   - `client.listen.v1.connect(...)` is called directly (no `await`) and returns an async context manager.
   - The connection object has `send_media(bytes)` (async), `start_listening()` (async, blocks until connection closes — must run as a background task concurrent with sends), and `on(EventType, callback)` (sync registration).
   - **`EventType` only has four members: `OPEN`, `MESSAGE`, `ERROR`, `CLOSE`.** There is no `EventType.UTTERANCE_END`. A later fix attempt tried to register this directly and crashed with `AttributeError` — confirming this the hard way.
   - **"UtteranceEnd" is a message *type*** (like "Results" and "Metadata"), delivered through the single `MESSAGE` event channel — not a distinct event category. The original message handler filtered for `type == "Results"` only, silently discarding every "UtteranceEnd" message for a long stretch of development.
   - **`speech_final` on "Results" messages never reliably fired** in real testing, despite being the documented end-of-utterance signal. The actual working signal is the separate "UtteranceEnd" message type, which requires `utterance_end_ms=1000` (or similar) to be passed at `connect()` time — without this parameter, "UtteranceEnd" messages never get sent at all.
   - `encoding="mulaw"` is valid and Twilio/Telnyx's native audio format needs **zero conversion** for the STT leg.
   - `model="nova-3"` has genuine, confirmed Arabic dialect support: `ar`, `ar-AE`, `ar-SA`, `ar-QA`, `ar-KW`, `ar-SY`, `ar-LB`, `ar-PS`, `ar-JO`, `ar-EG`, **`ar-SD`** (Sudan), `ar-MA`, `ar-DZ`, `ar-TN`, `ar-IQ`, `ar-TD`, `ar-IR`.

7. **`deepgram_tts.py`'s `generate()` call is an async generator, not an awaitable returning a single response object.** Original code did `await generate(...)` then tried to read a response object directly; had to be fixed to `async for chunk in generate(...)` to correctly collect audio chunks. This exact uncertainty had been flagged in the code's own comments as "inferred, not introspected, sanity-check this" — and the flag turned out to be justified; it was indeed wrong on the first attempt.

8. **Original single-stream Arabic auto-detection bug (the headline bug of the whole project).** Every call opened *one* Deepgram STT connection, hardcoded to `language="en"`, because `session.language` is `None` before any detection has happened. Since Arabic is a monolingual model that never returns a `languages` field, and the connection was already locked to English before any detection logic could run, **Arabic had structurally never worked at the STT layer** — this was suspected as the top-priority bug in a formal handoff brief and was **confirmed true** by a subsequent investigation.

9. **The fix: dual-stream language arbitration.** Each call now opens **both** an English and an Arabic Deepgram STT connection simultaneously, feeding the same caller audio to both. The first utterance is scored on both sides using transcript confidence. Tuned constants (arrived at over three live-testing iterations):
   - `DETECTION_GRACE_S = 0.4` — wait this long if only one side has spoken so far.
   - `FRAGMENT_TIMEOUT_S = 3.0` — watchdog backstop.
   - `DECISION_MARGIN = 0.15` — below this confidence gap, the decision is **provisional**: reply goes out in the higher-scoring language, but both streams stay open and the next utterance re-arbitrates (up to `MAX_ARBITRATION_ROUNDS = 3`, then best-effort lock).
   - **`MIN_LOCK_TEXT_CHARS = 12` — never hard-lock on a short utterance, regardless of confidence gap.** Real incident that necessitated this: a caller said «يا أهلاً» (an Arabic greeting); the English model misheard it as "Yeah. Hi." at 0.98 confidence, versus the *correct* Arabic reading at only 0.86 — and would have wrongly locked English permanently. Greetings are near-homophones across this language pair and are almost always the very first thing a caller says, so this guard specifically protects the highest-risk moment for a wrong lock.
   - On lock: `session.language` is set (sticky for the rest of the call), the losing stream is closed, and the winning stream's already-buffered transcript becomes the first real user turn — no audio is lost and the caller never has to repeat themselves.
   - If Arabic would win arbitration but Arabic TTS is currently known-dead (see item 15), the system deliberately locks English instead — **never lock a language it structurally cannot speak back in.**
   - Dialect can be biased via `STT_LANGUAGE_AR` env var (default `ar`; e.g. `ar-EG`, `ar-SA`).
   - **Live-verified**, including real Gulf-dialect speech («طحت», «ابشر») transcribing correctly.
   - **Known limitation:** no mid-call language switching after final lock (architecture keeps only one STT stream alive post-lock).

10. **Deepgram Aura TTS has zero Arabic support** — confirmed directly against current docs (Aura-2 covers English, Spanish, Dutch, French, German, Italian, Japanese only). This is a **permanent platform limitation**, not a bug to try to work around by forcing Aura to handle Arabic text.

11. **Groq deprecated its entire Llama chat model lineup** (confirmed announcement, June 17, 2026): `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `meta-llama/llama-4-scout`, `qwen/qwen3-32b` — all gone from the platform. Recommended replacements were `openai/gpt-oss-20b` (fast tier) and `openai/gpt-oss-120b`/`qwen/qwen3.6-27b` (quality tier). **Chose `openai/gpt-oss-20b`** based on Hassan's own benchmark comparison (~963 tok/s, ~0.73s TTFT vs. Qwen's ~8x higher cost and slower response for the 30–50 token replies this project actually produces) — matches the project's stated "latency above all else" priority.

12. **Original ElevenLabs Arabic TTS setup:** `eleven_flash_v2_5` model, `output_format="ulaw_8000"` (matches Telnyx directly, zero conversion needed) — chosen over Cartesia (faster, but weak Arabic) and Voxtral (better blind-test Arabic preference, but requires self-hosting, ruled out as out-of-scope for a 12-day solo timeline, flagged as a possible future v2 idea). Requires an `ELEVENLABS_VOICE_ID_AR` env var with deliberately **no hardcoded default** — Arabic voice quality varies significantly by voice choice, and picking one requires actually listening, not guessing.

13. **ElevenLabs replaced as the *default* Arabic TTS provider by Groq-hosted Orpheus**, after live testing hit a real 402 `paid_plan_required` error — **ElevenLabs free-tier API keys cannot use library voices**, meaning Arabic callers heard total silence while the system's own logs claimed the reply had been "spoken" (a genuinely dangerous silent-failure class for a first-aid tool). Hassan asked for a free alternative. Facts about the replacement, verified across multiple independent sources (Groq's own changelog, docs, blog post, plus third-party integrations):
    - Groq deprecated `playai-tts` and `playai-tts-arabic` (announced Dec 23, 2025), replaced platform-wide by Orpheus models from Canopy Labs.
    - `canopylabs/orpheus-arabic-saudi` offers six voices: **abdullah, fahad, sultan** (male), **lulwa, noura, aisha** (female).
    - **Only supports `response_format="wav"`** — the Python SDK's `"mulaw"` literal option is for the legacy `playai` models only, not Orpheus. The new `groq_tts.py` service converts WAV → PCM → 8kHz mono mu-law locally via `audioop`/`audioop-lts`.
    - 200-character input cap per request → required a word-boundary text splitter.
    - Billed on the **same `GROQ_API_KEY`** as the LLM (~$40 per 1M characters, per third-party reporting).
    - **Free-tier limits, measured live: 100 requests/day, 1200 tokens(~chars)/minute.** One sentence = one request; concurrent synthesis bursts (from the pipelined-TTS architecture, see items 17–18) tripped 429 storms with 6–15 second retry-after delays per sentence. Fixed by serializing Orpheus requests through a semaphore (`GROQ_TTS_CONCURRENCY`, default 1 — recommend bumping to 3 after upgrading to Groq's paid Developer tier). This serialization is inaudible in practice: playback runs ~3s/sentence while synthesis only takes ~0.5–1s.
    - **Hassan personally auditioned all six voices** using a new diagnostic script (`scripts/test_arabic_tts.py`) and chose **`abdullah`**. `GROQ_TTS_VOICE_AR=abdullah` is set in `.env`; note `config.py`'s default is `aisha` — the `.env` value overrides it, so do not "fix" the code default back to match.
    - One-time human step required: the Groq org admin must accept model terms once at console.groq.com, or the API returns a 400 `model_terms_required` error (this is treated as a *permanent*-class error in the health/degradation ladder, see item 15).
    - **Accepted limitation:** Saudi dialect only — an Egyptian caller hears a Saudi accent (cosmetic tradeoff, explicitly accepted).
    - Provider is switchable via `TTS_PROVIDER_AR` env var (`groq` is now default; `elevenlabs` remains fully available, including its no-default-voice safety guard).

14. **Groq LLM occasionally returned fully empty completions**, repeatedly, especially on short filler caller input ("Wait.", "Oh my god.", "Do I push on it with the cloth or what"). This caused **genuine dead silence** for the caller multiple times in live testing — including once immediately after a caller asked *"Am I done?"* following knife removal from a bleeding wound, a real safety-relevant moment for this specific use case.
    - **Mitigation (kept permanently, even after root cause was found):** retry the Groq call once; if still empty, fall back to a spoken canned prompt ("Sorry, I didn't catch that. Can you say that again?") rather than silence.
    - **Root cause (found in a later fix cycle):** `openai/gpt-oss-20b` is a *reasoning* model, defaulting to `reasoning_effort="medium"`. Reasoning tokens counted against the original `max_completion_tokens=300` budget — so on short, low-content filler input, the model could burn its entire token budget on internal reasoning before emitting any actual content token. **Fix:** `reasoning_effort="low"`, cap raised to 640, plus a `if not chunk.choices: continue` guard (choices-less trailing stream chunks were separately aborting replies). Zero empty completions observed afterward, including retesting the exact original trigger phrases.

15. **The fallback speech synthesis itself failed once in live testing** — `httpx.RemoteProtocolError: Server disconnected without sending a response` — while trying to synthesize the *fallback* message after a Groq empty-reply, meaning the caller got total silence with no further recourse at all. Fixed with a dedicated retry specifically on the fallback speech path (2 attempts) — this is the safety net's own safety net.

16. **A process-level TTS health/degradation ladder** (`_tts_health["ar_dead"]`) was added, triggered by *permanent-class* errors only (HTTP 401/402/403/404, or Groq's 400 `model_terms_required`) — deliberately **not** triggered by 429s, which are temporary and recover within the rate-limit window on their own. Effects when Arabic is marked dead: no wasted retries on permanent errors, one loud actionable log line naming the exact fix, later calls in the same process run English-only (the Arabic STT stream isn't even opened — never try to detect a language the system structurally can't speak back in), arbitration never locks Arabic while marked dead, and if a call somehow already locked to Arabic generates reply text but produces zero playable audio, an **English** notice is spoken instead ("having technical trouble speaking Arabic, let's continue in English") — with memory recording the notice that was *actually heard*, not the silently-failed original reply.

17. **Responsiveness problem #1 — fully sequential TTS across sentences.** Each sentence's *entire* round trip (synthesis **plus** real-time-paced playback duration) was awaited before the next sentence's synthesis even began. Measured live: a 6-sentence severe-bleeding escalation reply took **~9.2 seconds** of pure sequential synthesis time before the last sentence was even queued for sending. **Fix:** decoupled synthesis from sending/pacing via a dedicated per-call audio-sender task plus an `asyncio.Queue` — reply generation now only *queues* finished audio and never awaits its playback; a separate sender loop paces sends independently. This same decoupling is what makes clean barge-in possible (see item 19).

18. **Responsiveness problem #2 (found after fixing #17) — sentences' TTS *synthesis calls* were still sequential relative to each other**, even after sending was decoupled: sentence N+1's synthesis didn't *start* until sentence N's synthesis finished. **Fix:** each sentence's TTS call is now fired as an `asyncio.Task` **immediately** as soon as Groq streams a complete sentence (not awaited inline inside the token loop); all tasks are collected and then awaited **in original order** afterward, before queuing audio — this lets the actual network calls overlap while guaranteeing correct playback order.

19. **Barge-in (interruption handling) implemented.** A new caller utterance arriving while Najda is still generating/speaking cancels the in-progress reply task (`asyncio.Task.cancel()`) and drains any audio already queued but not yet sent. Confirmed working in real live testing. **Later extended:** a caller interrupting *after* generation had finished but *before* playback had finished previously still got overlapping speech — fixed so a new utterance **always** drains queued-but-unsent audio, regardless of whether generation is still in progress. The remaining interruption granularity is one sentence in flight (~2–4s) — you can't un-say a sentence already mid-synthesis or mid-send.

20. **The watchdog/timeout safety net was originally dead code.** The "~3 seconds of no new fragments → flush anyway" check lived *inside* the `async for transcript in stream.receive_transcripts()` loop body — which only re-executes when Deepgram sends a genuinely *new* message. If the caller went completely silent, that loop body never ran again, meaning the exact safety net that existed to catch total silence could never fire in that scenario. **Fixed:** replaced with a real, independent per-call watchdog `asyncio.Task` (polling every 0.5s) that runs regardless of whether new transcripts arrive, covering both the language-detection phase and normal turns.

21. **Pre-synthesized bilingual greeting.** The fixed opening line ("Hi, I'm here to help. Can you tell me what happened?") was going through the full Groq+TTS round trip on *every single call*, for a line that never changes — costing a real, measured ~2 seconds of dead air after the caller's first "Hello?" for content that didn't need to be dynamically generated at all. **Fix:** the greeting (both languages) is pre-synthesized and cached in-process, pre-warmed at application startup via a lifespan hook, and queued instantly at call start with zero Groq/TTS dependency — recorded as an assistant turn in memory so the LLM's own history correctly reflects that it already greeted. (One lock-contention bug surfaced during this fix: a failing Arabic prewarm could delay the *entire* greeting by ~3s waiting on a shared lock — fixed so already-cached languages queue immediately and only a genuinely missing/failed language waits.)

22. **KB scenario-matching bugs** (`app/prompts/kb_loader.py`):
    - **A genuinely dangerous misroute, live-observed:** a car-crash trauma description («رجل دهسته سيارة، مغمى عليه ويتنفس» — "a man was hit by a car, unconscious, breathing") matched **KB_Choking** instead of the correct **KB_CPR**, due to two stacked causes: an overly generic shared keyword «تنفس»/"breathing" present in both scenarios' keyword lists, *combined with* **OS-dependent file-matching order** — Windows sorts file paths case-insensitively, so `KB_Choking` happened to beat `KB_CPR` on the Windows dev machine, while Linux would give the *opposite* result for the identical input. This meant the bug's actual live behavior would have differed between development and any Linux-based production deployment. **Fixed:** removed breathing-family keywords from both scenarios' lists entirely; added «مغمى»/«أغمي» (colloquial "passed out" — what real callers actually say) specifically to CPR; kept "cannot breathe"/"can't breathe" (with contraction normalization) specifically for choking; added an explicit case-insensitive sort so file-matching order is now identical on every OS regardless of filesystem quirks.
    - **Arabic orthographic normalization** added for both incoming transcript text and the keyword lists themselves: strips harakat/tatweel diacritics (U+064B–U+0652, U+0670, U+0640), unifies hamza-seated alef variants → ا, ى → ي, ة → ه. This directly addresses a substring-matching fragility that had been explicitly flagged (but not yet fixed) as a known limitation when `kb_loader.py` was first written.
    - **Remaining known limitations:** matching is still fundamentally substring-based (an LLM-side scenario classifier would be the proper structural upgrade if more misroutes surface); the matched scenario is "sticky" for the rest of a call once set; there is currently **no KB coverage at all** for trauma/road accidents, stroke, seizures, poisoning, or drowning — these fall through to the generic router (which safely defers to real emergency services, but offers no specific guidance). Authoring more `knowledge/KB_*.yaml` files is a pure content task; the schema itself is proven and stable.

23. **Model output quirks discovered and mitigated** (all live-observed, not hypothetical):
    - **Within-reply exact-duplicate sentence suppression:** the LLM once looped an entire 9-sentence reply *twice* within a single completion (18 sentences total → 18 parallel TTS calls fired at once → tripped provider 429 rate limits). Fixed by deduplicating exact-duplicate sentences within a single reply before firing any TTS tasks.
    - **"Speakability" filter:** drops sentences containing no letters or digits at all (a lone `")"` character was causing a TTS 400 error), and drops sentences that are majority-wrong-script for the call's current language (a stray English parenthetical aside — e.g. *"(Note: I ask one question at a time...)"* — was literally being spoken aloud by the Arabic voice mid-call). Dropped sentences are also excluded from conversation memory, so the LLM's own history doesn't reference lines the caller never actually heard.

24. **Sentence-level audio LRU cache** added (128 entries, ~4MB cap, applies across **all** TTS providers). First-aid dialogue by its nature repeats fixed phrases — e.g. the KB-scripted escalation line *"Call emergency services now. Do not stop what you are doing."* appeared 3+ times within a single real call. Cache hits now cost zero API quota and zero latency (visible in logs as "TTS done in 0.00s").

25. **Concurrent-call handling and diagnostics.** A warning is now logged whenever more than one call is simultaneously active — this was discovered to almost always mean a **stale browser dialer tab** left open during single-tester WebRTC demo/testing (directly observed: two simultaneous calls both listening to the same live mic and talking over each other, which was very confusing to debug before this warning existed). Concurrent calls **are** architecturally fully supported (all state is keyed by `call_sid` and properly cleaned up in `handle_call_end`) — the real-world ceiling is shared per-account rate limits across the providers, not a code limitation.

26. **Misc reliability/logging hygiene fixes:**
    - Caller-hangup races (`WebSocketDisconnect`, and Starlette's "close message has been sent" `RuntimeError`) now log one clean info line instead of a full stack trace.
    - The `/telnyx-token` router (WebRTC test-call auth) existed in the codebase but was **never actually mounted** in `main.py` — every WebRTC test-auth attempt 404'd. Fixing the mount also surfaced a *second*, previously-latent bug: a missing `telnyx_telephony_credential_id` field in `config.py` that would have caused a 500 error the moment the route was actually reachable. Both fixed together.
    - STT connections are retried once each on initial connect failure; if *both* the English and Arabic streams fail to connect entirely, a spoken failure notice is used instead of a call that's silently just dead; if one stream dies mid-call it's dropped once cleanly (no repeated per-20ms log spam); if all streams die mid-call, a spoken notice fires once.
    - Any turn where reply *text* was successfully generated but *zero audio* actually made it into the send queue now speaks a "having trouble" notice instead of silently succeeding with nothing audible — this exact silent-success scenario was observed live during the ElevenLabs-402 period, before the health/degradation ladder in item 16 existed to catch it.

27. **Naturalness/anti-repetition fix (the §5 task) — implemented, pending live verification.** The conversation felt rigid/scripted, and Najda repeated the exact same KB steps verbatim if the caller asked the same thing twice, even right after giving them. Root cause: the full KB content (triage question, all branch steps, the general-knowledge Q&A block) was injected fresh and identically into the system prompt **every single turn**, with no signal telling the model what it had already said — and the persona's "use them as your primary guide" framing actively encouraged recitation. Not a pipeline bug (STT/TTS/arbitration all confirmed working). Three-part fix, applied to both languages:
    - **Persona instructions** (`system_en.txt` / `system_ar.txt`): new "natural conversational acknowledgments" bullet; injected KB content reframed as *background knowledge to speak in one's own words, not a script to recite word-for-word*; explicit already-given-steps handling (acknowledge briefly, paraphrase, or ask what's unclear rather than re-reciting); and an explicit **escalation carve-out** — escalation phrasing is the one thing that SHOULD repeat (safety-critical), so "don't repeat" never suppresses a repeated "call emergency services now."
    - **State tracking** (`memory.py`, `voice.py`): per-call `_given_steps` set (parallel to `_history`, cleared alongside it), populated only via the same success signal item 26 uses — steps are marked "given" only on a turn whose reply audio actually made it out, never on a silent-failure path. Marking uses `detect_delivered_branches()` (word-overlap heuristic, `STEP_OVERLAP_THRESHOLD=0.35`, `BRANCH_DELIVERY_THRESHOLD=0.4`), so only branches the reply actually verbalized get marked — a triage-only turn marks nothing. Deliberately conservative: under-detection → harmless restatement (safe direction); over-detection could make the model skip a safety-critical step.
    - **`format_kb_for_prompt`** (`kb_loader.py`) now takes `given_steps` + `current_transcript`: it lists already-given steps as an explicit "do NOT repeat these verbatim" block, and it **excludes escalation-identical steps from that block at display time** — every KB branch lists its `escalation_phrase` as its literal last step (verified via grep across all 8 files), so the escalation text would otherwise land in the "don't repeat" list. The general-knowledge Q&A block is no longer injected unconditionally: it's trimmed to the single closest-matching entry, and only when the caller's current utterance contains a substantive match (tokens `len >= 4` after normalization — stops "Hello? Is anyone there?" sharing "is" with a CPR question from injecting an irrelevant entry).

28. **Ungrounded-reply length cap + within-reply near-dup prompt fix — implemented, pending live verification.** Live-observed (Aug 2 headache call, `scenario=None` — headache has no KB file): an unbounded 9-sentence reply not only asked the same question twice in reworded form, it ran into `GROQ_TTS_CONCURRENCY=1`'s 1200-token/min budget on its own and produced 16–23s single-sentence TTS latency from stacked 429 retry-after waits — long enough a real caller would likely hang up mid-reply. Diagnosis of the repetition half: a post-hoc **string-similarity filter is the wrong tool** — "هل تشرب ماء كافٍ؟" vs "هل شربت كميات كافية من الماء اليوم؟" share almost no literal word tokens (شرب/شربت, ماء/الماء, كافٍ/كافية are different inflections) and only ~0.1 character-trigram Jaccard, so any threshold that catches them would flag genuinely distinct sentences. Paraphrase defeats that kind of matching. Real fix, two parts:
    - **Length cap** (`voice.py`): `MAX_SENTENCES_NO_SCENARIO = 4` caps *ungrounded* (`scenario_hint is None`) replies only — enforced inside `_fire_sentence`, with a `break` out of the Groq token stream once capped. A matched scenario's scripted steps are **never** truncated (safety content untouched). `scenario_hint` is threaded through both `_stream_and_queue_reply` call sites in `_generate_reply`. Drop-filtered (exact-dup / unspeakable) sentences don't count toward the cap.
    - **Prompt-level source fix** (both persona files): new "how you talk" bullet — *within a single reply, don't ask about the same thing twice, even reworded* — Arabic mirror in the same informal register.
    - A character-n-gram near-duplicate backstop filter was designed and **explicitly not shipped**: its ~0.1 overlap on the actual motivating pair gives it a high false-negative rate on the exact bug it would exist for, so it could offer false confidence (see the §7.6 "consider capping reply length" note, now effectively superseded for the ungrounded path by this cap).

29. **Word-boundary keyword matching + triage-question tracking — fixes for the two bugs the §5/§6 live repeat-question test (Aug 2 WebRTC call) actually surfaced.** The long-awaited live test happened and confirmed the naturalness fix working (Gulf-dialect "فهمت." / "طيب، أشنو صار بالضبط؟" fired naturally; consecutive questions were genuinely paraphrased) — but it also exposed two real holes, both now fixed:
    - **Bug A — scenario misroute via substring match.** At 00:51:43 the caller said "لا لا لن تحصل اي صدمة فقط كنت نائم وصحيت ولدي وجع راس جدا رهيب" ("no trauma, I was sleeping and woke with a terrible headache") and it matched KB_Bleeding.yaml. Root cause: the fallback keyword دم (blood) is literally the middle two letters of صدمة (trauma/shock), and matching was raw substring search (`_normalize(kw) in text`) — so "no trauma" silently matched "blood," locking a plain headache call into an unresolvable bleeding-triage loop. This was exactly the failure mode the module docstring's LIMITATION note had predicted. **Fix** (`kb_loader.py`): `match_scenario` now uses `_keyword_matches()` — word-boundary regex `\b…\b` (patterns cached in `_KEYWORD_PATTERN_CACHE`), not substring. Verified on this Python 3.14 build: `re` treats Arabic letters as `\w` (Unicode-aware), so `\bدم\b` does NOT match inside صدمة but DOES match "يوجد دم على الجرح". **Accepted tradeoff, deliberately strict:** inflected/cliticized forms that substring matching previously caught are now safe misses → generic router ("ask what happened"), never a misroute — e.g. Arabic definite-article الدم, possessives قلبي, ب-forms بالدم, English "burning" vs keyword "burn". (An optional `(?:ال)?` definite-article tolerance was verified to work but **not** shipped — the original proposal was chosen.) Phrase keywords like "cannot breathe" unaffected (\b only checks span ends).
    - **Bug B — triage question repeated verbatim (a real gap in §27).** At 00:52:03 the model asked KB_Bleeding's exact triage question; the caller's answer STT-garbled into "ليس هناك لذيذ" (mis-heard, not a code bug); at 00:52:09 the model asked the identical question again — byte-identical, hitting the TTS cache (0.00s, no synthesis call). Root cause: `detect_delivered_branches()` only tracks `branch.steps`; the triage question lives in a separate KB field with its own always-fire instruction, and this call never reached branch steps, so §27 was never in a position to help. **Fix**: new `detect_triage_delivered()` (word-overlap of the triage question vs reply at `STEP_OVERLAP_THRESHOLD`) folded into the **same** `_given_steps` set in `voice.py`; `format_kb_for_prompt` now reframes the triage instruction once asked — "If the caller's last answer didn't clearly indicate which situation applies, do NOT repeat this question word-for-word — ask a short, differently worded clarifying follow-up" — telling the model what to do with an answer it couldn't classify instead of letting it default back to repetition.
    - **Not exercised by the live test:** `MAX_SENTENCES_NO_SCENARIO` — every `scenario=None` reply stayed at 1–2 sentences. Still worth a call that deliberately drags out an ungrounded topic.
    - **Status:** implemented and offline-verified — `tests/test_contraction.py` (all 10 cases incl. the live-incident + Arabic regressions) and `tests/test_local.py` (tests 1–2, now including the same Arabic regressions, `detect_triage_delivered` checks, and the triage-reframe check) green; an end-to-end stubbed `_generate_reply` run confirmed a triage-only reply marks the triage question (and nothing else) into `_given_steps`. Pending a live re-test: (a) "لا صدمة، وجع راس" call must NOT route to KB_Bleeding; (b) an unclassifiable/garbled triage answer must produce a paraphrased clarifying follow-up, not a verbatim repeat.

30. **TTS-hardening follow-ups (three bundled fixes) — implemented, pending live verification:**
    - **Per-provider HTTP timeouts** (`PROVIDER_TIMEOUT_SECONDS=60`, default). Every REST provider client now passes `timeout=` at construction: Groq TTS + LLM, Deepgram TTS, ElevenLabs. Deepgram STT excluded (WebSocket; app-level keepalive + `FRAGMENT_TIMEOUT_S` cover idle). **SDK-verified at source level** that a bare float becomes `httpx.Timeout(float)` → per-operation connect/read/write/pool, and the read timeout is **per-chunk** for streaming — so a long LLM stream survives as long as tokens keep arriving, while a genuinely hung request dies at 60s instead of hanging the reply task forever. (Deepgram's default was already 60; ElevenLabs' was 240 — this unifies them.)
    - **Barge-in memory fix** — the anti-repetition `_given_steps` marking (item 27) had a hole: on barge-in the `CancelledError` propagated straight out of `_stream_and_queue_reply`, skipping `memory.add_turn`/`mark_steps_given`, so steps the caller *heard* before interrupting were never marked. Fix: the relay now records each sentence into a `delivered` list in lockstep with queueing (`task_queue` items are `(sentence, task)`); both `CancelledError` handlers subtract the still-unsent tail (`q.qsize()`, which `_flush_utterance` is about to drain anyway) and record the exact-delivered subset via the new `_record_delivered_turn` helper (also used by the normal path). **Precision note:** queue = this reply's only producer, so unsent = tail of `delivered`; the one race (a lazy greeting part slipping in during the drain→reply-registration window) fails SAFE — over-truncation under-marks (false-negative "given"), never the false-positive that suppresses a safety re-statement.
    - **TTS health ladder generalized to English.** `_tts_health` now tracks `{"ar_dead", "en_dead"}`; `_note_tts_failure` marks per-language (with per-language hints incl. the correct Orpheus model-terms URL); language arbitration is now **symmetric** (`en_dead` → lock `ar` if available) — the old asymmetry encoded "English uses trustworthy Aura, Arabic is fragile," which died when English moved onto the same Groq Orpheus provider with a documented permanent-failure history; fallback/notice branches skip doomed TTS attempts when that language is dead (record the turn without audio instead); greeting prewarm + the empty-reply retry both skip dead languages.
    - **Verification:** `py_compile` + `tests/test_local.py` (5/5) green; live smoke OK (AR TTS 0.57s, LLM stream 0.42s through the new `timeout=` clients); a stub run confirmed `_record_barge_in_delivered` subtracts exactly the queued-but-unsent sentences. **Pending live:** a barge-in call — interrupt mid-reply, confirm the next turn does NOT re-ask a step the caller already heard, and that partial-reply steps appear in `_given_steps` logs.

---

## 5. RESOLVED — the naturalness/anti-repetition fix (implemented this cycle)

**Root cause:** the full KB content (triage question, all branch steps, general-knowledge Q&A) was injected fresh and identically into the system prompt every single turn, with no signal telling the model what it had already said; the persona's "use them as your primary guide" framing encouraged verbatim recitation. Not a pipeline bug — STT/TTS/arbitration all confirmed working.

**Fix (all three priorities, both languages):**
1. **Prompt-level:** anti-repetition + natural-acknowledgment instructions added to `system_en.txt` and `system_ar.txt`; injected KB content reframed as background knowledge ("not a script to recite word-for-word"); explicit escalation carve-out so "don't repeat" never suppresses a repeated "call emergency services now."
2. **State-tracking (root cause):** per-call "steps already given" tracked in `memory.py::_given_steps`, injected into the prompt as a direct signal via `kb_loader.format_kb_for_prompt(given_steps=...)`, marked only on the item-26 audio-success path via `detect_delivered_branches()` (branch-aware word overlap; escalation-identical steps display-excluded).
3. **General-knowledge trimming:** the full Q&A block is gone; only the single best-matching entry is injected, and only when the caller's utterance substantively matches (token-length filter).

**Status:** implemented and offline-verified — `tests/test_local.py` (tests 1–2) and `tests/test_contraction.py` green; ad-hoc checks confirmed triage-only turns mark nothing, full/loose severe recitations mark the severe branch, escalation never appears in the "already given" block, "tourniquet?" injects its Q&A entry, and "I cut my hand"/"Hello? Is anyone there?" inject nothing. **The live repeat-question test ran on Aug 2 and the naturalness behavior itself passed** (Gulf-dialect acknowledgments + genuinely paraphrased consecutive questions) — but the same call surfaced two new bugs: an Arabic scenario misroute (substring keyword match: دم inside صدمة) and a verbatim triage-question repeat on an STT-unclassifiable answer. **Both are fixed and offline-verified in §4.29**; the remaining step is a **follow-up live re-test** of those two fixes before §6 is updated. A related within-reply variant (near-duplicated question inside one ungrounded reply, and the 429 storm it caused) is fixed separately by the ungrounded-reply length cap — see §4.28.

---

## 6. What's confirmed verified (via real phone calls and/or WebRTC test calls)

- Full bidirectional Telnyx streaming (send + receive), including the self-echo filter.
- Dual-stream language arbitration — all paths: immediate lock, provisional→lock, timeout-based decision, and specifically the homophone-protection guard (replaying the real «يا أهلاً» incident).
- Gulf-dialect Arabic STT transcribing correctly.
- Groq Orpheus Arabic TTS end-to-end, including voice selection.
- Full KB triage flows in both languages (bleeding severe/embedded-object branch, fractures closed branch, and others).
- Escalation-phrase repetition across a call (exercising the sentence audio cache).
- Barge-in (interruption mid-reply).
- Greeting served instantly from cache.
- Watchdog-based flush when the caller goes silent.
- The empty-completion fix (retested against the exact original trigger phrases — confirmed zero recurrence).
- The full TTS degradation ladder (tested during a real ElevenLabs-402 day).
- Concurrent-call handling (via an accidental two-tab test).
- Clean call hangups (no stack-trace spam).

---

## 7. Open items / known limitations (not bugs — deliberate, documented gaps)

1. **Production deployment — DONE (Oracle Cloud, Aug 2026).** Live host: Oracle Cloud me-riyadh-1, Ampere ARM instance, Reserved Public IP, Caddy HTTPS reverse proxy, `docker-compose.yml` binding `127.0.0.1:8000` with `APP_ENV=production` forced. The DuckDNS name is retained as the stable public hostname so the Telnyx webhook URL is unchanged. (Supersedes the original EC2 plan — us-east-1, t2/t3.micro, swap-before-build — which is no longer relevant.) Remaining real items: (a) narrow the broad Oracle Security List rule (currently all TCP, stateless, `0.0.0.0/0`); (b) Deepgram EU-endpoint switch — considered, deferred; (c) sync = manual `git pull` on the server only.
2. **CloudWatch auto-stop + AWS Budgets billing alert** — these were AWS-specific (Day 11 of the original plan) and no longer apply on Oracle Cloud; verify Oracle's equivalents (compute auto-stop + billing alert) if cost control matters. Status: unconfirmed.
3. **Demo video recording + final README polish** (Day 12 of the original plan) — status unconfirmed, likely still pending.
4. **Groq Developer tier upgrade recommended** for Arabic TTS — the free tier (100 requests/day, 1200 tokens/minute) stalls multi-sentence replies under real conversational load. The paid tier is **not** a smarter model, only higher limits/billing — same underlying model either way. After upgrading, bump `GROQ_TTS_CONCURRENCY` from 1 to 3.
5. **Demo hygiene notes:** use exactly **one** dialer tab at a time (the concurrent-call warning exists specifically to catch accidental duplicates); use a headset — the *protocol-level* self-echo (Telnyx echoing our own TTS back to us) is filtered, but *acoustic/room* echo (speakers picking up on a live mic) is not, and would make Najda hear herself.
6. **Optional prompt tuning, never applied:** consider banning parenthetical asides entirely in both persona files (everything gets spoken aloud — a stray parenthetical is a real, observed failure mode, see item 23), and consider capping reply length more strictly (saves API quota and latency; the model currently tends to over-talk relative to its own "short sentences" instruction).
7. **KB breadth** — only 8 scenarios exist; no coverage for trauma/road accidents, stroke, seizures, poisoning, or drowning. Authoring more `knowledge/KB_*.yaml` files is a pure content task (schema is proven).
8. **No mid-call language switching** after the initial arbitration lock (the architecture keeps only one STT stream alive post-lock).
9. **Pause-based mid-sentence flush tradeoff** — the ~1 second `utterance_end_ms` endpointing window is a deliberate tuning choice; callers naturally recover from it, but it's tunable at the Deepgram `connect()` call if ever revisited.
10. **`@telnyx/webrtc@2.27.8` hangup heap-leak freeze** — SDK-internal, profiler-proven not ours (single click → 5s, 8,613/10,157 samples in the bundle's `Ue`/`methodFactory`/`_captureHangupCallerStack`, heap 4.8MB→1.69GB; 2.27.8 is the latest stable, no upgrade path). Trial harness with knobs in `docs/app.js` (`?noReports`/`?noReconnect`/`?maxReconnect=N`/`?delayBeforeHangup=N`/`?callDebug`/`?debugOutput`/`?debug`); bug report drafted. Pending: live trial matrix to pick the workaround knob, then bake default + file upstream.

---

## 8. Testing conventions used throughout this project

- **Cheap live testing:** Telnyx WebRTC browser calling (SIP Connection with credentials-based auth, dial via webrtc.telnyx.com) — hits the identical `/voice` webhook and `/ws/media` WebSocket as a real PSTN call, at a small fraction of the cost of a real international call from Malaysia.
- **Offline test harnesses** with stubbed SDKs (fake scripted STT streams per language, fake Groq/TTS responses) covering: arbitration scenarios (decisive win, near-tie/provisional, one-sided evidence, timeout-based decision, and specifically a regression test replaying the real homophone incident), the 402-degradation ladder, barge-in queue-draining, both fallback paths, sentence deduplication, the speakability filter, KB matching (including a deliberately reversed file order to simulate Windows-vs-Linux sort differences), and WAV→mulaw conversion against synthetic audio. `tests/test_local.py` holds the project's own KB/prompt-level tests.
- **Per-call diagnostic logging** deliberately includes: language-arbitration decision inputs and confidence scores, per-sentence TTS synthesis timings, and time-to-first-Groq-token — specifically so that future responsiveness or correctness debugging can be done from logs alone, the same way every fix in §4 above was actually diagnosed.

---

## 9. How to continue this project (guidance for whoever picks this up next)

1. **Read this entire document before writing or suggesting any code.** Section 4 exists specifically so you don't re-discover an already-fixed bug or accidentally revert a deliberate correction because it "looks unusual."
2. **Treat the confirmed-facts list (§4) as settled.** If something in the code looks strange — a Telnyx field in an unexpected place, a Deepgram event handled in an odd way, a specific model name that seems outdated — check this document first. It is very likely a deliberate fix for a confirmed live bug, not an oversight.
3. **The single most valuable practice across this entire project has been: verify third-party API behavior directly (via search or SDK introspection) rather than assume it from training data or documentation alone.** Every major bug in §4 was caught specifically because something was actually checked — the Groq Llama deprecation, Telnyx's protocol differences from Twilio, Deepgram's exact SDK method names and event types, the real ElevenLabs Agent-API community bugs, and the Groq Orpheus replacement facts were all confirmed this way, not assumed. Continue this practice for anything new.
4. **Hassan does not have Claude execute code directly in most sessions** — he runs a separate local coding agent (opencode) and/or uses Claude Workspace with Fable to apply changes, then pastes back real call logs for review. Expect to receive pasted logs/tracebacks and produce either (a) diagnosis + complete, ready-to-paste code changes, or (b) a clear, scoped brief for a coding agent to implement — matching the collaborative pattern this entire project has used so far.
5. **This project doubles as a resume/interview artifact.** The README already documents several of the corrections in §4 as an explicit "Engineering Decisions & Corrections" section, specifically because catching and correctly explaining real mistakes is more impressive to a technical interviewer than a plan that was never tested against reality. Continuing to accumulate genuine, well-reasoned corrections (like §5's current task) is itself part of the project's value — don't shy away from documenting a new fix clearly just because it reveals something was previously wrong.
6. **§5 (naturalness/repetition fix) is implemented and live-tested** — the Aug 2 call confirmed the naturalness behavior but surfaced two new bugs (Arabic scenario misroute, verbatim triage repeat) now fixed in §4.29; the word-boundary variant (دم inside صدمة) is fixed and offline-verified. After the **follow-up live re-test** of those fixes passes and §6 is updated, the remaining work is the production housekeeping in §7.1 (Security List, etc.) — deployment itself is DONE on Oracle Cloud.

---

## 10. Environment variable inventory

All settings are read from `.env` by `config.py` (pydantic-settings, `SettingsConfigDict(env_file=".env")`). Field names are lowercase in code; env vars are uppercase (pydantic-settings is case-insensitive). `settings.validate_required([...])` raises `RuntimeError` with the missing names on the specific flows that need them. **No secret values in this inventory — only names, defaults, and purpose.**

| Env var | Default | Purpose | Where used |
|---|---|---|---|
| `TELNYX_API_KEY` | (required) | Telnyx REST auth | Telnyx integration / webhook validation |
| `TELNYX_PHONE_NUMBER` | (required) | The +1 number assigned to the TeXML Application | webhook + media WS |
| `TELNYX_TELEPHONY_CREDENTIAL_ID` | `""` | WebRTC test-call JWT auth only (Mission Control → API Keys → Telephony Credentials). **Not** needed for the phone path. Missing → clear error from `/telnyx-token`. | `app/routes/telnyx_token.py` |
| `DEEPGRAM_API_KEY` | (required) | STT only (TTS moved to Groq Orpheus) | `deepgram_stt.py` |
| `STT_LANGUAGE_AR` | `"ar"` | Arabic dialect bias for Nova-3 (e.g. `ar-EG`, `ar-SA`; full list in `language.py::ARABIC_DIALECT_CODES`) | deepgram STT + arbitration |
| `GROQ_API_KEY` | (required) | LLM **and** TTS for both languages (Orpheus — same key, no extra account) | `groq_llm.py`, `groq_tts.py` |
| `TTS_PROVIDER_AR` | `"groq"` | `"groq"` (Orpheus, default) or `"elevenlabs"` | `language.py::get_tts_provider()` |
| `GROQ_TTS_VOICE_AR` | `"aisha"` (config default) | Orpheus Arabic voice. **Live `.env` sets `abdullah`** (user's choice — see §4.13). Options: abdullah/fahad/sultan/lulwa/noura/aisha | `groq_tts.py` |
| `GROQ_TTS_VOICE_EN` | `"austin"` (config default) | Orpheus English voice (`canopylabs/orpheus-v1-english`) | `groq_tts.py` |
| `GROQ_TTS_CONCURRENCY` | `1` | Orpheus request concurrency. Keep 1 on free tier (1200 tokens/min budget 429-storms on bursts); set 3 after Groq Developer tier. | `groq_tts.py` semaphore |
| `GROQ_REASONING_EFFORT` | `"low"` | LLM reasoning effort (low/medium/high). "low" = fastest; "medium" was proposed to test Arabic grammar quality (observed non-word hallucinations at low). | `groq_llm.py` |
| `PROVIDER_TIMEOUT_SECONDS` | `60` | Per-provider HTTP timeout (Groq TTS/LLM, Deepgram TTS, ElevenLabs). httpx float → per-operation, read is per-chunk so long LLM streams survive; kills genuinely hung requests. Deepgram STT (WebSocket) excluded. | `groq_tts.py`, `groq_llm.py`, `deepgram_tts.py`, `elevenlabs_tts.py` |
| `ELEVENLABS_API_KEY` | `""` | Only when `TTS_PROVIDER_AR=elevenlabs` | `elevenlabs_tts.py` |
| `ELEVENLABS_VOICE_ID_AR` | `""` | Only for ElevenLabs path; **deliberately no default** — empty → `RuntimeError` | `elevenlabs_tts.py` |
| `APP_ENV` | `"development"` | Enables uvicorn `reload` in dev; `docker-compose.yml` forces `production` | `run.py`, Docker |
| `PUBLIC_BASE_URL` | `""` | Public webhook URL (ngrok/DuckDNS); `ws_base_url()` derives the `wss://` URL | `main.py` / Telnyx webhook config |

Note: `audioop-lts` is a pip requirement (Python 3.13+ removed `audioop` from stdlib) — not an env var, but required for the WAV→mulaw conversion used by `groq_tts.py`.

---

## 11. How to run and test

**Dev server (Windows/local):**
```
python run.py
```
Runs uvicorn on `app.main:create_app` (factory), host `0.0.0.0`, port **8000**, auto-reload when `APP_ENV=development`. Needs `.env` populated (see §10) and the Telnyx number pointed at a webhook reachable from the internet (ngrok during local dev) for live calls.

**Production (Oracle Cloud):** `bash scripts/start.sh` — wraps `run.py` with logging/readiness checks; `docker-compose.yml` forces `APP_ENV=production`. On the server the app binds `127.0.0.1:8000` behind a Caddy HTTPS reverse proxy. Deploy sync = `git pull` + `docker compose up -d --build` (manual).

**Offline tests / harnesses (plain scripts, no pytest needed):**
- `python tests/test_local.py` — KB matching + prompt assembly suite (project's own tests; also covered by the stub-SDK harnesses described in §8).
- `python tests/test_contraction.py` — contraction-normalization quick check ("can't breathe" → matches "cannot breathe").
- `python tests/check_telnyx.py` — Telnyx connectivity/credential diagnostic (currently untracked).
- `python scripts/test_arabic_tts.py` — 4-stage Orpheus diagnostic: env → API reachability + model visibility → LLM ping → synthesize all six voices to `voice_samples/*.wav` (includes the exact mulaw conversion). Works from any cwd.

**Cheap live testing:** Telnyx WebRTC browser calling (SIP Connection, credentials auth, via webrtc.telnyx.com) — same `/voice` webhook + `/ws/media` WebSocket as a real PSTN call. Use **one** dialer tab and a headset (§7.5).
