"""
Prompt assembly.

Day 9 status: KB content now loads from real YAML files (see
kb_loader.py) and is matched to the caller's actual situation via
keyword detection, replacing the Day 1 stub that concatenated all 8
placeholder files on every turn. Only the matched scenario's content
is injected, keeping the prompt focused and token-cheap.
"""

from app.core.language import get_system_prompt_path
from app.models.schemas import Turn
from app.prompts import kb_loader


def load_system_prompt(language: str) -> str:
    path = get_system_prompt_path(language)
    return path.read_text(encoding="utf-8")


def load_knowledge_context(scenario_hint: str | None, language: str) -> str:
    """
    scenario_hint is a matched KB filename (e.g. "KB_Bleeding.yaml") if
    one's been detected this call, or None if not yet known.
    """
    if scenario_hint:
        return kb_loader.format_kb_for_prompt(scenario_hint, language)
    return kb_loader.format_generic_router(language)


def build_messages(
    language: str,
    history: list[Turn],
    scenario_hint: str | None = None,
) -> list[dict]:
    system_content = (
        load_system_prompt(language)
        + "\n\n"
        + load_knowledge_context(scenario_hint, language)
    )

    messages = [{"role": "system", "content": system_content}]
    messages.extend({"role": turn.role, "content": turn.content} for turn in history)
    return messages
