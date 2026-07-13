"""
Per-call conversation memory.

Basic storage (add/get/clear) is fully implemented now since it doesn't
depend on any external API. Summarization (to keep long calls within
Groq's context budget) is stubbed until Day 7.
"""

from app.models.schemas import Turn

# In-memory store keyed by CallSid. Fine for a single-process demo;
# would need Redis or similar for a real multi-worker deployment.
_history: dict[str, list[Turn]] = {}

SUMMARIZE_AFTER_TURNS = 10


def add_turn(call_sid: str, role: str, content: str) -> None:
    _history.setdefault(call_sid, []).append(Turn(role=role, content=content))

    if len(_history[call_sid]) > SUMMARIZE_AFTER_TURNS:
        _maybe_summarize(call_sid)


def get_history(call_sid: str) -> list[Turn]:
    return _history.get(call_sid, [])


def clear(call_sid: str) -> None:
    _history.pop(call_sid, None)


def _maybe_summarize(call_sid: str) -> None:
    """
    Collapse older turns into a single summary turn to keep token
    count bounded on long calls.

    Stubbed until Day 7 — currently a no-op, so history grows
    unbounded (fine for short demo calls, not fine for long ones).
    """
    # TODO Day 7: summarize _history[call_sid][:-N] via Groq,
    # replace with a single condensed "assistant" turn.
    pass
