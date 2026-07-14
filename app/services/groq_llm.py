"""
Groq LLM client.

IMPORTANT: Groq deprecated its entire Llama chat lineup (llama-3.1-8b-instant,
llama-3.3-70b-versatile) in June 2026 — confirmed via Groq's own deprecations
page. This project uses openai/gpt-oss-20b instead: Groq's fastest hosted
model (~963 tok/s, ~0.73s TTFT), chosen deliberately over qwen/qwen3.6-27b
per the latency-above-all-else priority for this project (see decision log —
qwen scores higher on general intelligence benchmarks but is ~8x the cost
and meaningfully slower; for 30-50 token first-aid replies, gpt-oss-20b's
speed advantage matters more than the benchmark gap).

Interface finalized now; implementation lands Day 4. This is already
streaming, so Day 10 doesn't need to change this file — it just needs
app/core/voice.py to consume the stream incrementally into TTS instead
of buffering full text first (once TTS exists on Day 5).
"""

from collections.abc import AsyncGenerator

from groq import AsyncGroq

from config import settings

# Fastest model on Groq as of this writing (July 2026) — see decision log
# above. One-line swap point if later testing (especially Arabic quality)
# favors a different model.
MODEL = "openai/gpt-oss-20b"

_client = AsyncGroq(api_key=settings.groq_api_key)


async def stream_completion(messages: list[dict]) -> AsyncGenerator[str, None]:
    """
    Streams a chat completion from Groq token-by-token.

    `messages` follows the standard OpenAI-style format:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}, ...]

    Yields text chunks as they're generated so the caller can start
    TTS before the full response is ready (once TTS is wired in on Day 5).
    """
    stream = await _client.chat.completions.create(
        model=MODEL,
        messages=messages,
        stream=True,
        max_completion_tokens=300,  # first aid replies should be short; keeps latency down
    )

    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
