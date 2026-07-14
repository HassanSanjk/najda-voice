"""
Per-call conversation memory.

Day 7 status: real summarization implemented. Older turns get condensed
into a single summary turn via Groq once a call exceeds
SUMMARIZE_AFTER_TURNS, keeping the most recent KEEP_RECENT_TURNS intact.

Summarization runs as a detached background task rather than inline —
blocking the live turn on a summarization call would add latency to
exactly the moment the caller is waiting on a reply, which conflicts
with this project's core latency priority. Because it's detached, a
race is possible: a new turn could be added while summarization is
still awaiting Groq. This is handled explicitly (see _summarize_and_replace)
by snapshotting history at the start and splicing back in anything
added during the await, rather than blindly overwriting.

If summarization itself fails for any reason, the full history is kept
as-is rather than silently losing anything — a bloated prompt is a
recoverable cost; losing conversation context mid-call is not.
"""

import asyncio
import logging

from app.models.schemas import Turn
from app.services.groq_llm import stream_completion

logger = logging.getLogger(__name__)

# In-memory store keyed by CallSid. Fine for a single-process demo;
# would need Redis or similar for a real multi-worker deployment.
_history: dict[str, list[Turn]] = {}

# call_sids currently being summarized — prevents triggering a second
# summarization task while one is already in flight for the same call.
_summarizing: set[str] = set()

SUMMARIZE_AFTER_TURNS = 10
KEEP_RECENT_TURNS = 4  # kept verbatim; everything older gets condensed


def add_turn(call_sid: str, role: str, content: str) -> None:
    _history.setdefault(call_sid, []).append(Turn(role=role, content=content))

    if len(_history[call_sid]) > SUMMARIZE_AFTER_TURNS and call_sid not in _summarizing:
        _summarizing.add(call_sid)
        asyncio.create_task(_summarize_and_replace(call_sid))


def get_history(call_sid: str) -> list[Turn]:
    return _history.get(call_sid, [])


def clear(call_sid: str) -> None:
    _history.pop(call_sid, None)
    _summarizing.discard(call_sid)


async def _summarize_and_replace(call_sid: str) -> None:
    try:
        # Snapshot at the moment we start — anything appended to
        # _history[call_sid] after this point (while we're awaiting
        # Groq below) needs to survive, not get overwritten.
        snapshot = list(_history.get(call_sid, []))

        if len(snapshot) <= KEEP_RECENT_TURNS:
            return  # nothing old enough to summarize (shouldn't normally hit this)

        older = snapshot[:-KEEP_RECENT_TURNS]
        recent = snapshot[-KEEP_RECENT_TURNS:]

        summary_text = await _summarize_turns(older)
        summary_turn = Turn(
            role="assistant",
            content=f"[Summary of earlier conversation: {summary_text}]",
        )

        current = _history.get(call_sid, [])
        # Anything added after our snapshot was taken — preserve it.
        added_during_summary = current[len(snapshot):]

        _history[call_sid] = [summary_turn] + recent + added_during_summary
        logger.info(
            f"[{call_sid}] summarized {len(older)} turns into 1 summary turn "
            f"({len(recent)} recent turns kept verbatim)"
        )

    except Exception:
        logger.exception(f"[{call_sid}] summarization failed, keeping full history")

    finally:
        _summarizing.discard(call_sid)


async def _summarize_turns(turns: list[Turn]) -> str:
    """
    Condenses a list of turns into a short summary via Groq. Keeps key
    facts (injury type, severity, steps already given) rather than a
    generic recap — the summary replaces real conversation context, so
    losing specifics here would degrade the agent's actual first-aid
    guidance on long calls.
    """
    conversation_text = "\n".join(f"{t.role}: {t.content}" for t in turns)

    messages = [
        {
            "role": "system",
            "content": (
                "Summarize the following first-aid conversation in 2-3 sentences. "
                "Preserve specific facts: the injury/condition, its severity, and "
                "any first-aid steps already given or actions the caller has taken. "
                "Do not add new advice or commentary — just condense what happened."
            ),
        },
        {"role": "user", "content": conversation_text},
    ]

    summary = ""
    async for token in stream_completion(messages):
        summary += token
    return summary.strip()
