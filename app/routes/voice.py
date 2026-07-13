"""
Twilio entrypoints.

POST /voice      -> Twilio calls this on incoming call; we respond with TwiML
                    that opens a <Connect><Stream> to /ws/media.
WS   /ws/media   -> Twilio Media Streams connects here; raw audio (mu-law)
                    flows in both directions over this socket.

POST /voice is fully working (Day 2). WS /ws/media has the full Twilio
event-handling shape wired up now, with STT/LLM/TTS logic stubbed in
app/core/voice.py until Days 3-10.
"""

import base64
import json

from fastapi import APIRouter, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.core.voice import handle_audio_chunk, handle_call_end, handle_call_start
from app.models.schemas import CallSession
from config import settings

router = APIRouter()


@router.post("/voice")
async def incoming_call(
    CallSid: str = Form(...),
    From: str = Form(None),
    To: str = Form(None),
):
    """
    Twilio webhook hit on incoming call. Must return TwiML (XML).
    Opens a bidirectional media stream to /ws/media for the actual
    conversation once Days 3+ logic is filled in.
    """
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
    """
    Twilio Media Streams WebSocket handler.

    Twilio sends a sequence of JSON events over this socket:
        {"event": "connected", ...}
        {"event": "start", "start": {"streamSid": ..., "callSid": ..., "mediaFormat": {...}}}
        {"event": "media", "media": {"payload": "<base64 mu-law audio>", ...}}  # repeated
        {"event": "stop", ...}

    Full event dispatch is wired up here now. The actual STT -> LLM -> TTS
    pipeline (app/core/voice.py) is stubbed until Days 3-7.
    """
    await websocket.accept()
    session: CallSession | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            event = json.loads(raw)
            event_type = event.get("event")

            if event_type == "connected":
                # Nothing to do yet — just confirms the socket is live.
                continue

            elif event_type == "start":
                start_data = event["start"]
                session = CallSession(
                    call_sid=start_data["callSid"],
                    stream_sid=start_data["streamSid"],
                )
                await handle_call_start(session)

            elif event_type == "media":
                if session is None:
                    continue  # guard against out-of-order events
                audio_bytes = base64.b64decode(event["media"]["payload"])
                reply_audio = await handle_audio_chunk(session, audio_bytes)
                if reply_audio:
                    await _send_media(websocket, session.stream_sid, reply_audio)

            elif event_type == "stop":
                if session:
                    await handle_call_end(session)
                break

    except WebSocketDisconnect:
        if session:
            await handle_call_end(session)


async def _send_media(websocket: WebSocket, stream_sid: str, audio_bytes: bytes) -> None:
    """
    Wraps outbound audio in the JSON envelope Twilio expects and sends
    it back over the same media WebSocket.
    """
    payload = base64.b64encode(audio_bytes).decode("utf-8")
    message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload},
    }
    await websocket.send_text(json.dumps(message))
