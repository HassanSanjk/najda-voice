"""
Groq LLM client (Llama 3).

Interface finalized now; implementation lands Day 4, streaming upgraded Day 10.
"""

from collections.abc import AsyncGenerator


async def stream_completion(messages: list[dict]) -> AsyncGenerator[str, None]:
    """
    Streams a chat completion from Groq token-by-token (or chunk-by-chunk).

    `messages` follows the standard OpenAI-style format:
        [{"role": "system", "content": ...}, {"role": "user", "content": ...}, ...]

    Yields text chunks as they're generated so the caller can start
    TTS before the full response is ready.
    """
    raise NotImplementedError("Implemented Day 4, streaming added Day 10")
    yield  # pragma: no cover — makes this a generator for type-checking
