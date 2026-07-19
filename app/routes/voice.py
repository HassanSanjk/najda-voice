"""
Twilio/Telnyx entrypoints.

Telnyx field name convention (confirmed from real call logs):
- "stream_id" (top-level, snake_case) -- NOT Twilio's "streamSid"
- "call_control_id" for call identification -- NOT Twilio's "callSid"
- start event is flat (no nested "start" sub-object like Twilio)
"""

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.core.voice import handle_audio_chunk, handle_call_end, handle_call_start
from app.models.schemas import CallSession
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/voice")
async def incoming_call(request: Request):
    form = await request.form()
    logger.info(f"[DIAGNOSTIC] /voice raw form data: {dict(form)}")

    ws_url = f"{settings.ws_base_url()}/ws/media"

    texml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you to Najda Voice.</Say>
    <Connect>
        <Stream url="{ws_url}"
                track="both_tracks"
                bidirectionalMode="rtp"
                bidirectionalCodec="PCMU" />
    </Connect>
</Response>"""
    return Response(content=texml, media_type="application/xml")


@router.websocket("/ws/media")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    session: CallSession | None = None

    async def send_audio_back(audio_bytes: bytes) -> None:
        if session and session.stream_sid:
            await _send_media(websocket, session.stream_sid, audio_bytes)

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                raise

            try:
                event = json.loads(raw)
                event_type = event.get("event")
            except Exception:
                logger.exception("failed to parse incoming WebSocket message, skipping")
                continue

            try:
                if event_type == "connected":
                    continue

                elif event_type == "start":
                    logger.info(f"[DIAGNOSTIC] start event full contents: {event}")
                    start_data = event.get("start", {})
                    session = CallSession(
                        call_sid=start_data.get("call_control_id", "unknown"),
                        stream_sid=event.get("stream_id", "unknown"),
                    )
                    logger.info(
                        f"[DIAGNOSTIC] session created: call_sid={session.call_sid}, "
                        f"stream_sid={session.stream_sid}"
                    )
                    await handle_call_start(session, send_audio_back)

                elif event_type == "media":
                    if session is None:
                        continue
                    media = event.get("media", {})
                    track = media.get("track")
                    if track == "outbound":
                        continue
                    media_payload = media.get("payload") or event.get("payload")
                    if media_payload:
                        audio_bytes = base64.b64decode(media_payload)
                        await handle_audio_chunk(session, audio_bytes)

                elif event_type == "stop":
                    break

                else:
                    logger.info(f"[DIAGNOSTIC] unrecognized event type '{event_type}': {event}")

            except Exception:
                logger.exception(f"error handling '{event_type}' event, continuing call")

    except WebSocketDisconnect:
        call_sid = session.call_sid if session else "unknown"
        logger.info(f"WebSocket disconnected (call_sid={call_sid})")

    finally:
        if session:
            await handle_call_end(session)


async def _send_media(websocket: WebSocket, _stream_id: str, audio_bytes: bytes) -> None:
    FRAME_SIZE = 160
    FRAME_INTERVAL = 0.02
    total_frames = (len(audio_bytes) + FRAME_SIZE - 1) // FRAME_SIZE
    loop = asyncio.get_running_loop()
    start = loop.time()

    for i in range(total_frames):
        frame = audio_bytes[i * FRAME_SIZE:(i + 1) * FRAME_SIZE]
        payload = base64.b64encode(frame).decode("utf-8")
        message = {
            "event": "media",
            "media": {
                "track": "outbound",
                "payload": payload,
            },
        }
        try:
            await websocket.send_text(json.dumps(message))
        except WebSocketDisconnect:
            # Normal when the caller hangs up while audio is still being
            # paced out — not an error worth a stack trace.
            logger.info(f"caller disconnected mid-send, dropping remaining {total_frames - i} frame(s)")
            return
        except RuntimeError as exc:
            if "close message has been sent" in str(exc):
                # Same hangup race, surfaced as RuntimeError by starlette
                # when a second sender hits the already-closed socket.
                logger.info(f"websocket already closed, dropping remaining {total_frames - i} frame(s)")
                return
            logger.exception(f"failed to send audio frame back to Telnyx")
            return
        except Exception:
            logger.exception(f"failed to send audio frame back to Telnyx")
            return
        expected_next = start + (i + 2) * FRAME_INTERVAL
        now = loop.time()
        delay = expected_next - now
        if delay > 0:
            await asyncio.sleep(delay)

    logger.info(f"sent {total_frames} audio frames back to Telnyx")
