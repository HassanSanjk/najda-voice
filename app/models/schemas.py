"""
Pydantic models used across routes, services, and core orchestration.
"""

from typing import Literal

from pydantic import BaseModel


# ---------- Twilio Media Streams (WS /ws/media) ----------
# Protocol reference: https://www.twilio.com/docs/voice/media-streams/websocket-messages
# NOTE: verify field names against Twilio's current docs before Day 3 —
# streaming APIs occasionally add/rename fields.

class MediaFormat(BaseModel):
    encoding: str  # e.g. "audio/x-mulaw"
    sampleRate: int
    channels: int


class StreamStartPayload(BaseModel):
    streamSid: str
    callSid: str
    mediaFormat: MediaFormat


class MediaPayload(BaseModel):
    track: str  # "inbound" (caller) or "outbound" (agent)
    chunk: str
    timestamp: str
    payload: str  # base64-encoded mu-law audio


# ---------- Internal session state ----------

class CallSession(BaseModel):
    call_sid: str
    stream_sid: str | None = None
    language: str | None = None  # "en" or "ar", set once detected
    is_active: bool = True


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class TranscriptChunk(BaseModel):
    text: str
    is_final: bool
    language: str | None = None
