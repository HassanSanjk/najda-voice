"""
Live verification of the claimed Groq Orpheus 429 rate-limit behavior.

Background: a call produced 16-23s single-sentence TTS latency,
attributed to a per-minute token budget producing 429 storms with 6s
retry-after waits. The free-tier limits are real (Groq docs:
canopylabs/orpheus-arabic-saudi = 10 RPM / 100 RPD / 1.2K TPM / 3.6K
TPD) and the SDK retry math can produce 6-15s per sentence on
persistent 429s (max_retries=2, honoring Retry-After <= 60s), but the
observed logs showed zero 429s and max 3.29s synthesis. This script
settles it empirically.

Approach: replay the burst scenario through the REAL production path
(app.services.groq_tts.synthesize, WAV->mulaw conversion included) with
an instrumented httpx client whose response hook records EVERY HTTP
response the SDK sees — including intermediate 429s and retry attempts.

Phases:
  0  baseline: one short sentence.
   A  burst replay: 9 KB sentences, sequential (production concurrency=1).
  B  same 9 sentences, concurrent (what raising GROQ_TTS_CONCURRENCY does).
  C  forced overload: 26 real KB sentences (~1360 chars, over the
     1200-TPM ceiling) fired concurrently — guarantees observing 429s
     and their retry-after latency.

Usage (run from the repo root):
  python scripts/verify_groq_tts_429.py --dry-run   # plan only, no API calls
  python scripts/verify_groq_tts_429.py --phase A   # run one phase
  python scripts/verify_groq_tts_429.py             # run all phases

Budget: ~1 + 9 + 9 + 19 = 38 requests, plus one attempt per SDK retry
(worst case ~90 total against the 100/day cap). Watch the phase output's
"remaining requests" line and stop early if it gets low.
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")  # Arabic sentences must print on Windows

import httpx
from groq import AsyncGroq

from config import settings
import app.services.groq_tts as groq_tts  # noqa: E402  (path setup above)

# --------------------------------------------------------------------------
# Payloads: real sentences from knowledge/KB_CPR.yaml (Arabic, adult + infant
# branches) and the longest general-knowledge answers. ~1 token per char
# (verified live: a 34-char Arabic greeting consumed 34 tokens).
# --------------------------------------------------------------------------

ADULT_STEPS = [
    "اضربه بقوة على كتفه واصرخ، هل أنت بخير؟",
    "انظر واستمع للتنفس لمدة عشر ثوانٍ فقط.",
    "إذا لم يكن يتنفس بشكل طبيعي، اطلب من أحد الاتصال بالإسعاف الآن.",
    "إذا كان جهاز الصدمات متوفراً، أرسل أحداً لإحضاره الآن.",
    "ضع كعب يدك في منتصف صدره.",
    "ضع يدك الأخرى فوقها وشابك أصابعك.",
    "اضغط بقوة وسرعة، بعمق خمسة سنتيمترات على الأقل.",
    "دع الصدر يرتفع بالكامل بين كل ضغطة.",
    "اعمل ثلاثين ضغطة، ثم نفسين إذا كنت مدرباً.",
    "إذا لم تكن مدرباً، استمر بالضغط فقط بدون توقف.",
    "استمر بسرعة ضغطتين في الثانية تقريباً.",
    "اتصل بالإسعاف الآن. لا تتوقف عما تفعله.",
]

INFANT_STEPS = [
    "اضرب قدمه برفق واصرخ باسمه بصوت عالٍ.",
    "انظر واستمع للتنفس لمدة عشر ثوانٍ فقط.",
    "إذا لم يكن يتنفس بشكل طبيعي، اطلب من أحد الاتصال بالإسعاف الآن.",
    "ضع إصبعين تحت خط الحلمتين مباشرة على صدره.",
    "اضغط بعمق أربعة سنتيمترات تقريباً، بقوة وسرعة.",
    "دع الصدر يرتفع بالكامل بين كل ضغطة.",
    "اعمل ثلاثين ضغطة، ثم نفسين لطيفين تغطي فمه وأنفه معاً.",
    "استمر بسرعة ضغطتين في الثانية تقريباً.",
    "اتصل بالإسعاف الآن. لا تتوقف عما تفعله.",
]

# Longest general-knowledge answers (real KB content) — used in phase C to
# push the token total past the 1200/min ceiling without a huge request count.
GK_ANSWERS = [
    "شغّله واتبع تعليماته الصوتية بالضبط. الصق اللواصق على الصدر العاري كما هو موضح بالصور. تأكد ألا يلمس أحد الشخص عند توصيل الصدمة.",
    "استمر فقط بالضغط على الصدر بدون توقف. الضغط وحده يساعد ويكون أفضل بكثير من عدم فعل شيء.",
    "اضغط بقوة، من الطبيعي أن تُكسر ضلوع. الضلع المكسور يشفى. لكن توقف الضغط قد يكلفه حياته.",
    "أعطِ نفسين اصطناعيين أولاً قبل أن تبدأ الضغط على الصدر. ثم استمر بالنمط المعتاد: ثلاثين ضغطة ونفسين.",
    "توقف إذا بدأ يتنفس أو يتحرك بنفسه، أو إذا وصل المسعفون وتولوا الأمر، أو إذا أصبحت منهكاً جداً للاستمرار.",
]

PHASES = {
    "0": ("baseline (1 sentence)", [ADULT_STEPS[0]], 1, False),
    "A": ("burst replay: 9 sentences, sequential", ADULT_STEPS[:9], 1, False),
    "B": ("same 9 sentences, concurrent", ADULT_STEPS[:9], 9, True),
    # 12 + 9 + 5 = 26 requests, ~1360 chars (~1360 tokens) — exceeds the
    # 1200-TPM ceiling AND the 10-RPM ceiling, so both limit dimensions are
    # exercised. Worst case with the SDK's 2 retries: ~78 attempts for this
    # phase alone — check "remaining requests" after phase B before running C.
    "C": ("forced overload: 26 sentences (~1360 chars) concurrent",
          ADULT_STEPS + INFANT_STEPS + GK_ANSWERS, 26, True),
}

# --------------------------------------------------------------------------
# Instrumentation: record every HTTP response the SDK sees (incl. retries).
# --------------------------------------------------------------------------

_events: list[dict] = []


async def _on_response(response: httpx.Response) -> None:
    h = response.headers
    _events.append({
        "ts": time.monotonic(),
        "status": response.status_code,
        "retry_after": h.get("retry-after"),
        "rem_reqs": h.get("x-ratelimit-remaining-requests"),
        "reset_reqs": h.get("x-ratelimit-reset-requests"),
        "rem_tokens": h.get("x-ratelimit-remaining-tokens"),
        "reset_tokens": h.get("x-ratelimit-reset-tokens"),
    })


_instrumented_client: AsyncGroq | None = None


def get_client() -> AsyncGroq:
    global _instrumented_client
    if _instrumented_client is None:
        http = httpx.AsyncClient(event_hooks={"response": [_on_response]})
        _instrumented_client = AsyncGroq(api_key=settings.groq_api_key, http_client=http)
    return _instrumented_client


def install_instrumented_client(concurrency: int) -> None:
    """Point the real production module at our instrumented client so the
    exact production code path (including the WAV->mulaw conversion) is
    what gets measured."""
    groq_tts._client = get_client()
    groq_tts._concurrency = asyncio.Semaphore(max(1, concurrency))


# --------------------------------------------------------------------------
# Phase execution + reporting
# --------------------------------------------------------------------------


async def run_phase(name: str, sentences: list[str], concurrency: int, concurrent: bool) -> dict:
    install_instrumented_client(concurrency)
    _events.clear()

    total_chars = sum(len(s) for s in sentences)
    print(f"\n=== Phase {name}: {PHASES[name][0]} | {len(sentences)} req | "
          f"~{total_chars} chars (~{total_chars} tokens est) | concurrency={concurrency} ===")

    results: list[tuple[int, str, float, int]] = []
    phase_start = time.monotonic()

    async def one(i: int, text: str) -> None:
        t0 = time.monotonic()
        try:
            audio = await groq_tts.synthesize(text, "ar")
            results.append((i, "ok", time.monotonic() - t0, len(audio)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Failed after the SDK's own retries ran out (or permanent error)
            results.append((i, f"FAIL {type(exc).__name__}: {str(exc)[:120]}", time.monotonic() - t0, 0))

    if concurrent:
        await asyncio.gather(*(one(i, s) for i, s in enumerate(sentences)))
    else:
        for i, s in enumerate(sentences):
            await one(i, s)

    elapsed = time.monotonic() - phase_start
    results.sort()

    # ---- per-request table
    print(f"\n{'#':>2} | {'result':<58} | {'dur(s)':>7} | {'audio':>7}")
    print("-" * 86)
    for i, res, dur, size in results:
        print(f"{i:>2} | {res:<58} | {dur:>7.2f} | {size:>7}")

    # ---- HTTP-level view (every response incl. SDK retries)
    http429 = [e for e in _events if e["status"] == 429]
    http_ok = [e for e in _events if e["status"] == 200]
    print(f"\nHTTP responses seen: {len(_events)} total "
          f"({len(http_ok)} x 200, {len(http429)} x 429, "
          f"{len(_events) - len(http_ok) - len(http429)} other)")
    if http429:
        retry_afters = sorted({e["retry_after"] for e in http429 if e["retry_after"]})
        print(f"429s: {len(http429)} | Retry-After values seen: {retry_afters or 'none'}")
        for e in http429[:6]:
            print(f"  429 @{e['ts'] - phase_start:6.1f}s retry-after={e['retry_after']} "
                  f"rem_reqs={e['rem_reqs']} rem_tokens={e['rem_tokens']}")
        if len(http429) > 6:
            print(f"  ... and {len(http429) - 6} more")

    # ---- phase summary
    durs = [d for _, _, d, _ in results if d > 0]
    ok_count = sum(1 for _, r, _, _ in results if r == "ok")
    print(f"\nPhase {name} summary: {ok_count}/{len(sentences)} synthesized | "
          f"total elapsed {elapsed:.1f}s | per-sentence dur: "
          f"min={min(durs):.2f}s avg={sum(durs) / len(durs):.2f}s max={max(durs):.2f}s" if durs
          else f"\nPhase {name} summary: {ok_count}/{len(sentences)} synthesized | total elapsed {elapsed:.1f}s")

    if _events:
        last = _events[-1]
        print(f"after phase: remaining requests={last['rem_reqs']} "
              f"(reset {last['reset_reqs']}), remaining tokens={last['rem_tokens']} "
              f"(reset {last['reset_tokens']})")

    return {"requests": len(sentences), "ok": ok_count, "429s": len(http429),
            "max_dur": max(durs) if durs else 0.0}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Groq Orpheus 429 rate-limit behavior live")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit (no API calls)")
    parser.add_argument("--phase", choices=list(PHASES), action="append",
                        help="run only this phase; repeatable; default = all")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no API calls will be made.\n")
        total_reqs = 0
        for name, (desc, sentences, conc, concurrent) in PHASES.items():
            chars = sum(len(s) for s in sentences)
            total_reqs += len(sentences)
            print(f"  Phase {name}: {desc}")
            print(f"    requests: {len(sentences)} | chars: {chars} (~{chars} tokens) | mode: {'concurrent' if concurrent else 'sequential'}")
            if name == "A":
                print("    sentences (real KB_CPR.yaml Arabic):")
                for i, s in enumerate(sentences):
                    print(f"      {i}: ({len(s)} ch) {s}")
        print(f"\n  Total requests if all phases run: ~{total_reqs} + up to 2 retries per 429.")
        print("  Daily cap: 100 (free tier). Watch 'remaining requests' after each phase.")
        return

    if not settings.groq_api_key:
        print("GROQ_API_KEY not set in .env — aborting.")
        sys.exit(1)

    # Preflight: one baseline request also warms nothing but gives us the
    # starting daily budget.
    phase0 = await run_phase("0", *PHASES["0"][1:])
    remaining = None
    if _events:
        remaining = int(_events[-1].get("rem_reqs") or -1)
    print(f"\npreflight: daily requests remaining = {remaining}")

    if remaining is not None and remaining < 60:
        print("WARNING: fewer than 60 requests left today — phase C alone may use ~57.")
        print("Consider Ctrl-C now and rerun with: --phase A --phase B")

    phases_to_run = args.phase or list(PHASES)
    for name in phases_to_run:
        if name == "0":
            continue  # already done above
        await run_phase(name, *PHASES[name][1:])
        await asyncio.sleep(2)

    print("\n=== DONE. Verdict: compare the per-sentence durations and 429 counts "
          "against the documented claim of 6-15s per sentence on 429 storms. ===")


if __name__ == "__main__":
    asyncio.run(main())
