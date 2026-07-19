"""
Arabic TTS audition + pipeline diagnostic for Groq Orpheus.

Runs four stages, each isolating a different failure mode:
  1. env      — is GROQ_API_KEY actually loaded?
  2. models   — can we reach api.groq.com at all, and does this account
                see the Orpheus models?
  3. llm ping — does a plain chat completion work (the path the live app
                already uses successfully)?
  4. tts      — synthesize the greeting in all six Saudi voices, and run
                the exact WAV -> 8kHz mu-law conversion live calls use.

Works from any directory:
    python scripts/test_arabic_tts.py
    python .\\test_arabic_tts.py        (from inside scripts/)

Outputs (in ./voice_samples/): ar_<voice>.wav to listen to, ar_<voice>.ulaw
(the exact bytes Telnyx would receive). Then set GROQ_TTS_VOICE_AR in .env.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# .env is loaded relative to the current working directory (pydantic-settings)
# — pin cwd to the project root BEFORE importing config so this script works
# no matter where it's launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from app.services import groq_tts  # noqa: E402
from app.services.groq_tts import MODEL, _wav_to_mulaw_8k  # noqa: E402
from config import settings  # noqa: E402

VOICES = ["abdullah", "fahad", "sultan", "lulwa", "noura", "aisha"]
TEXT = "مرحباً، أنا نجدة. أخبرني ماذا حدث."
OUT_DIR = PROJECT_ROOT / "voice_samples"


def _describe(exc: BaseException) -> str:
    """Full error description including the underlying cause chain that
    SDK wrapper exceptions (e.g. 'Connection error.') hide."""
    parts = []
    seen = 0
    e: BaseException | None = exc
    while e is not None and seen < 5:
        status = getattr(e, "status_code", None)
        body = getattr(e, "body", None)
        detail = f"{type(e).__name__}: {body or e}"
        if status:
            detail = f"HTTP {status} {detail}"
        parts.append(detail)
        e = e.__cause__ or e.__context__
        seen += 1
    return " <- ".join(parts)


async def main() -> None:
    print(f"Project root: {PROJECT_ROOT}")

    # --- stage 1: env ---
    key = settings.groq_api_key
    if not key:
        print("[FAIL] GROQ_API_KEY is empty — .env not found or key missing.")
        print("       Expected .env at:", PROJECT_ROOT / ".env")
        return
    print(f"[OK]   GROQ_API_KEY loaded (…{key[-4:]})")

    # --- stage 2: reachability + model visibility ---
    try:
        models = await groq_tts._client.models.list()
        ids = sorted(m.id for m in models.data)
        orpheus = [m for m in ids if "orpheus" in m]
        print(f"[OK]   api.groq.com reachable — {len(ids)} models visible")
        if orpheus:
            print(f"[OK]   Orpheus models visible to this account: {orpheus}")
        else:
            print("[WARN] No Orpheus models in this account's model list!")
            print("       Full list:", ids)
            print("       Check console.groq.com -> Settings -> Model Permissions.")
    except Exception as exc:
        print(f"[FAIL] cannot reach api.groq.com from this session:")
        print(f"       {_describe(exc)}")
        print("       (VPN/proxy/firewall? Compare with the running app, which")
        print("        reaches Groq successfully in your call logs.)")
        return

    # --- stage 3: LLM ping (known-good path in the live app) ---
    try:
        resp = await groq_tts._client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": "Say OK."}],
            max_completion_tokens=20,
        )
        print(f"[OK]   LLM ping: {resp.choices[0].message.content!r}")
    except Exception as exc:
        print(f"[FAIL] LLM ping failed: {_describe(exc)}")

    # --- stage 4: Orpheus TTS, all voices ---
    OUT_DIR.mkdir(exist_ok=True)
    print(f"\nModel: {MODEL}\nText:  {TEXT}\n")
    ok = 0
    for voice in VOICES:
        started = time.monotonic()
        try:
            response = await groq_tts._client.audio.speech.create(
                model=MODEL, voice=voice, input=TEXT, response_format="wav",
            )
            wav_bytes = await response.read()
            mulaw = _wav_to_mulaw_8k(wav_bytes)
            elapsed = time.monotonic() - started

            (OUT_DIR / f"ar_{voice}.wav").write_bytes(wav_bytes)
            (OUT_DIR / f"ar_{voice}.ulaw").write_bytes(mulaw)
            secs = len(mulaw) / 8000
            print(
                f"[OK]   {voice:<9} synth {elapsed:.2f}s  wav {len(wav_bytes):>7}B  "
                f"mulaw {len(mulaw):>6}B (~{secs:.1f}s audio)"
            )
            ok += 1
        except Exception as exc:
            print(f"[FAIL] {voice:<9} {_describe(exc)}")

    print(f"\n{ok}/{len(VOICES)} voices synthesized. Samples in: {OUT_DIR}")
    if ok:
        print("Listen to the .wav files, then set GROQ_TTS_VOICE_AR=<voice> in .env")


if __name__ == "__main__":
    asyncio.run(main())
