"""
Browser demo page endpoints (skeleton).

Read-only GETs backing docs/index.html:
- /active-calls: which Telnyx calls are live right now (and the newest one),
  so the page can subscribe to the right call without guessing from order.
- /transcript/{call_sid}: the in-call transcript, falling back to a bounded
  snapshot of recently finished calls (memory is cleared per call on hangup).
"""

import logging

from fastapi import APIRouter

from app.core import memory
from app.core.voice import (
    active_call_sids,
    get_ended_scenario,
    get_ended_transcript,
    get_scenario,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/active-calls")
async def active_calls():
    sids = active_call_sids()
    return {
        "calls": [{"call_sid": sid, "scenario": get_scenario(sid)} for sid in sids],
        "newest": sids[-1] if sids else None,
    }


@router.get("/transcript/{call_sid}")
async def transcript(call_sid: str):
    turns = memory.get_history(call_sid) or get_ended_transcript(call_sid)
    return {
        "call_sid": call_sid,
        "scenario": get_scenario(call_sid) or get_ended_scenario(call_sid),
        "turns": [{"role": t.role, "content": t.content} for t in turns],
    }
