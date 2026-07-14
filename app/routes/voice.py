"""
Twilio entrypoints.

Day 6 update: the WebSocket message loop now isolates per-message
handling in try/except so one malformed/unexpected event doesn't crash
the whole call ungracefully, and cleanup (handle_call_end) is now
guaranteed via try/finally rather than only firing on a clean
WebSocketDisconnect.
"""

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.core.voice import handle_audio_chunk, handle_call_end, handle_call_start
from app.models.schemas import CallSession
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/voice")
async def incoming_call(
    CallSid: str = Form(...),
    From: str = Form(None),
    To: str = Form(None),
):
    ws_url = f"{settings.ws_base_url()}/ws/media"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you to Najda Voice.</Say>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/media")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    session: CallSession | None = None

    async def send_audio_back(audio_bytes: bytes) -> None:
        if session and session.stream_sid:
            await _send_media(websocket, session.stream_sid, audio_bytes)

    try:
        while True:
            event_type = None
            try:
                raw = await websocket.receive_text()
                event = json.loads(raw)
                event_type = event.get("event")
            except WebSocketDisconnect:
                raise  # handled by the outer except below
            except Exception:
                logger.exception("failed to parse incoming WebSocket message, skipping")
                continue

            try:
                if event_type == "connected":
                    continue

                elif event_type == "start":
                    start_data = event["start"]
                    session = CallSession(
                        call_sid=start_data["callSid"],
                        stream_sid=start_data["streamSid"],
                    )
                    await handle_call_start(session, send_audio_back)

                elif event_type == "media":
                    if session is None:
                        continue
                    audio_bytes = base64.b64decode(event["media"]["payload"])
                    await handle_audio_chunk(session, audio_bytes)

                elif event_type == "stop":
                    break

                else:
                    logger.debug(f"unrecognized Twilio event type: {event_type}")

            except Exception:
                logger.exception(f"error handling '{event_type}' event, continuing call")

    except WebSocketDisconnect:
        call_sid = session.call_sid if session else "unknown"
        logger.info(f"WebSocket disconnected (call_sid={call_sid})")

    finally:
        # Guaranteed cleanup regardless of exit path: normal "stop" event,
        # disconnect, or any exception that somehow still escaped above.
        if session:
            await handle_call_end(session)


async def _send_media(websocket: WebSocket, stream_sid: str, audio_bytes: bytes) -> None:
    """
    Sends audio back to Twilio in 20ms mu-law frames (160 bytes at 8kHz),
    paced in real time.
    """
    FRAME_SIZE = 160
    FRAME_DURATION_S = 0.02

    for i in range(0, len(audio_bytes), FRAME_SIZE):
        frame = audio_bytes[i:i + FRAME_SIZE]
        payload = base64.b64encode(frame).decode("utf-8")
        message = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload},
        }
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            logger.exception("failed to send audio frame back to Twilio (caller likely hung up)")
            return  # stop sending remaining frames — connection is gone
        await asyncio.sleep(FRAME_DURATION_S)
