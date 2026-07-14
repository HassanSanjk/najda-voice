"""Local test suite — run with: python tests/test_local.py"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_kb_loader():
    print("=" * 60)
    print("TEST 1: KB Loader")
    print("=" * 60)
    from app.prompts.kb_loader import match_scenario, format_kb_for_prompt, get_kb_names

    names = get_kb_names()
    print(f"  Available scenarios: {names}")

    tests = [
        ("I cut my hand", "en", "KB_Bleeding.yaml"),
        ("someone is choking", "en", "KB_Choking.yaml"),
        ("my house is on fire and I got burned", "en", "KB_Burns.yaml"),
        ("I think my arm is broken", "en", "KB_Fractures.yaml"),
        ("hello how are you", "en", None),
        ("help me my child cannot breathe", "en", "KB_Choking.yaml"),
    ]

    all_pass = True
    for text, lang, expected in tests:
        result = match_scenario(text, lang)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  [{status}] match_scenario({text!r}, {lang}) = {result} (expected {expected})")

    kb = format_kb_for_prompt("KB_Bleeding.yaml", "en")
    has_triage = "triage" in kb.lower() or "question" in kb.lower() or "bleeding" in kb.lower()
    has_steps = "step" in kb.lower() or "pressure" in kb.lower() or "1." in kb or "2." in kb
    print(f"  [{'PASS' if has_triage else 'FAIL'}] KB_Bleeding.yaml contains triage content")
    print(f"  [{'PASS' if has_steps else 'FAIL'}] KB_Bleeding.yaml contains steps")
    if not (has_triage and has_steps):
        all_pass = False

    print(f"\n  KB formatted output preview:\n{kb[:600]}")
    print(f"\n  TEST 1: {'ALL PASSED' if all_pass else 'SOME FAILED'}")
    return all_pass


def test_prompt_builder():
    print("\n" + "=" * 60)
    print("TEST 2: Prompt Builder")
    print("=" * 60)
    from app.prompts.prompt_builder import build_messages, load_knowledge_context
    from app.models.schemas import Turn

    # With scenario hint
    history = [Turn(role="user", content="I burned my hand on the stove")]
    msgs = build_messages("en", history, scenario_hint="KB_Burns.yaml")
    print(f"  Messages count: {len(msgs)}")
    print(f"  System prompt length: {len(msgs[0]['content'])} chars")
    has_emergency = "burns" in msgs[0]["content"].lower()
    print(f"  [{'PASS' if has_emergency else 'FAIL'}] System prompt contains 'burns' emergency topic")

    # Without scenario hint (generic router)
    msgs2 = build_messages("en", history, scenario_hint=None)
    has_router = "no specific emergency" in msgs2[0]["content"].lower() or "available topics" in msgs2[0]["content"].lower()
    print(f"  [{'PASS' if has_router else 'FAIL'}] Generic router prompt when no scenario matched")

    # Arabic
    history_ar = [Turn(role="user", content="لدي حرق في يدي")]
    msgs3 = build_messages("ar", history_ar, scenario_hint="KB_Burns.yaml")
    has_arabic = any("\u0627" <= c <= "\u064a" for c in msgs3[0]["content"])
    print(f"  [{'PASS' if has_arabic else 'FAIL'}] Arabic system prompt contains Arabic characters")

    print(f"  System prompt preview:\n{msgs[0]['content'][:500]}")
    passed = has_emergency and has_router and has_arabic
    print(f"\n  TEST 2: {'PASSED' if passed else 'FAILED'}")
    return passed


def test_groq_llm():
    print("\n" + "=" * 60)
    print("TEST 3: Groq LLM Streaming")
    print("=" * 60)
    from app.services.groq_llm import stream_completion

    async def run():
        msgs = [
            {"role": "system", "content": "You are a first aid assistant. Reply in 1-2 short sentences only."},
            {"role": "user", "content": "I cut my finger and it is bleeding."},
        ]
        print("  Streaming response: ", end="", flush=True)
        tokens = []
        async for token in stream_completion(msgs):
            tokens.append(token)
            print(token, end="", flush=True)
        print()
        full = "".join(tokens)
        print(f"  Token count: {len(tokens)}")
        print(f"  Response length: {len(full)} chars")
        return len(full) > 10

    passed = asyncio.run(run())
    print(f"\n  TEST 3: {'PASSED' if passed else 'FAILED'}")
    return passed


def test_deepgram_tts():
    print("\n" + "=" * 60)
    print("TEST 4: Deepgram TTS (English)")
    print("=" * 60)
    from app.services.deepgram_tts import synthesize

    async def run():
        text = "Apply direct pressure to the wound with a clean cloth."
        print(f"  Synthesizing: {text!r}")
        audio = await synthesize(text, language="en")
        print(f"  Audio bytes received: {len(audio)} bytes")
        if len(audio) > 0:
            with open("test_audio.raw", "wb") as f:
                f.write(audio)
            print("  Saved to test_audio.raw")
        return len(audio) > 100

    passed = asyncio.run(run())
    print(f"\n  TEST 4: {'PASSED' if passed else 'FAILED'}")
    return passed


def test_fastapi_startup():
    print("\n" + "=" * 60)
    print("TEST 5: FastAPI Startup + /health")
    print("=" * 60)
    import subprocess, time, urllib.request, signal, os

    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    print(f"  Started server (PID {proc.pid}), waiting 3s...")
    time.sleep(3)

    try:
        resp = urllib.request.urlopen("http://localhost:8000/health", timeout=5)
        body = resp.read().decode()
        print(f"  /health status: {resp.status}")
        print(f"  /health body: {body}")
        passed = resp.status == 200 and "ok" in body.lower()
    except Exception as e:
        print(f"  /health failed: {e}")
        passed = False
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        print(f"  Server stopped")

    print(f"\n  TEST 5: {'PASSED' if passed else 'FAILED'}")
    return passed


if __name__ == "__main__":
    results = []
    results.append(("KB Loader", test_kb_loader()))
    results.append(("Prompt Builder", test_prompt_builder()))
    results.append(("Groq LLM", test_groq_llm()))
    results.append(("Deepgram TTS", test_deepgram_tts()))
    results.append(("FastAPI Startup", test_fastapi_startup()))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}: {name}")
    all_passed = all(p for _, p in results)
    print(f"\n  Overall: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
