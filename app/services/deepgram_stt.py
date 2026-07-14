"""
Deepgram Streaming STT client.

(Day 3 header comment unchanged — see original for the verified SDK
facts this implementation relies on.)

Day 6 update: callbacks (_on_message, _on_error, _on_close) are now
defensive — they're invoked synchronously by the SDK's internals, so a
bug inside one of them could otherwise propagate somewhere unexpected.
Wrapped and logged instead.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator

from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType

from config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 8000


class DeepgramSTTStream:
    def __init__(self, language: str = "en") -> None:
        self._client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)
        self._language = language

        self._connection_ctx = None
        self._connection = None
        self._listen_task: asyncio.Task | None = None
        self._transcript_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        self._connection_ctx = self._client.listen.v1.connect(
            model="nova-3",
            encoding="mulaw",
            sample_rate=SAMPLE_RATE,
            language=self._language,
            interim_results=True,
            smart_format=True,
        )
        self._connection = await self._connection_ctx.__aenter__()

        self._connection.on(EventType.MESSAGE, self._on_message)
        self._connection.on(EventType.ERROR, self._on_error)
        self._connection.on(EventType.CLOSE, self._on_close)

        self._listen_task = asyncio.create_task(self._connection.start_listening())

    def _on_message(self, message) -> None:
        try:
            if getattr(message, "type", None) != "Results":
                return
            alternatives = message.channel.alternatives
            if not alternatives or not alternatives[0].transcript:
                return
            alt = alternatives[0]
            self._transcript_queue.put_nowait({
                "text": alt.transcript,
                "is_final": message.is_final,
                "language": alt.languages[0] if alt.languages else None,
            })
        except Exception:
            logger.exception("error handling Deepgram message")

    def _on_error(self, error) -> None:
        # No reconnect logic yet — out of scope for this demo. Logged
        # clearly so a dead STT connection is visible in the logs rather
        # than just manifesting as "the agent stopped responding."
        logger.error(f"Deepgram STT error: {error}")

    def _on_close(self, _event) -> None:
        logger.debug("Deepgram STT connection closed")
        self._transcript_queue.put_nowait(None)

    async def send_audio(self, chunk: bytes) -> None:
        await self._connection.send_media(chunk)

    async def receive_transcripts(self) -> AsyncGenerator[dict, None]:
        while True:
            transcript = await self._transcript_queue.get()
            if transcript is None:
                break
            yield transcript

    async def close(self) -> None:
        if self._connection:
            try:
                await self._connection.send_close_stream()
            except Exception:
                logger.debug("send_close_stream failed (connection likely already closed)")
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._connection_ctx:
            try:
                await self._connection_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("error exiting Deepgram connection context")
