"""
Deepgram Streaming STT client.

Interface finalized now; implementation lands Day 3. Uses Nova-3,
which has verified native Arabic dialect support (see app/core/language.py
for the confirmed dialect code list) alongside English.
"""

from collections.abc import AsyncGenerator

from config import settings


class DeepgramSTTStream:
    """
    Wraps a single streaming STT connection to Deepgram for one call.

    Usage (once implemented):
        stream = DeepgramSTTStream()
        await stream.connect()
        await stream.send_audio(chunk_bytes)
        async for transcript in stream.receive_transcripts():
            ...
        await stream.close()
    """

    def __init__(self) -> None:
        self._api_key = settings.deepgram_api_key
        self._connection = None  # will hold the websocket connection

    async def connect(self) -> None:
        """Open the streaming connection to Deepgram (model=nova-3, language=multi or ar/en)."""
        raise NotImplementedError("Implemented Day 3")

    async def send_audio(self, chunk: bytes) -> None:
        """Forward one mu-law audio chunk from Twilio to Deepgram."""
        raise NotImplementedError("Implemented Day 3")

    async def receive_transcripts(self) -> AsyncGenerator[dict, None]:
        """
        Yields transcript events as they arrive, e.g.:
            {"text": "...", "is_final": bool, "language": "ar-SD"}
        """
        raise NotImplementedError("Implemented Day 3")
        yield  # pragma: no cover — makes this a generator for type-checking

    async def close(self) -> None:
        """Close the Deepgram connection."""
        raise NotImplementedError("Implemented Day 3")
