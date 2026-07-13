"""
Prompt assembly.

File-reading and history-formatting plumbing is implemented now since
it doesn't depend on any external API. KB-scenario matching (choosing
which of the 8 knowledge/ files is relevant to the caller's situation)
is stubbed until Day 9, once the KB content itself is filled in.
"""

from pathlib import Path

from app.core.language import get_system_prompt_path
from app.models.schemas import Turn

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"


def load_system_prompt(language: str) -> str:
    path = get_system_prompt_path(language)
    return path.read_text(encoding="utf-8")


def load_knowledge_context(scenario_hint: str | None = None) -> str:
    """
    Loads relevant KB content to inject into the prompt.

    Stubbed scenario matching for now — Day 9 will implement real
    keyword/intent matching (e.g. detecting "bleeding" in the transcript
    -> load only KB_Bleeding.txt). Currently concatenates all KB files,
    which works but is token-expensive; fine for early pipeline testing,
    must be replaced before Day 9 is done.
    """
    contents = []
    for kb_file in sorted(KNOWLEDGE_DIR.glob("KB_*.txt")):
        contents.append(kb_file.read_text(encoding="utf-8"))
    return "\n\n".join(contents)


def build_messages(
    language: str,
    history: list[Turn],
    scenario_hint: str | None = None,
) -> list[dict]:
    """
    Assembles the final message list sent to Groq:
        [system prompt + KB context] + [conversation history]
    """
    system_content = (
        load_system_prompt(language) + "\n\n" + load_knowledge_context(scenario_hint)
    )

    messages = [{"role": "system", "content": system_content}]
    messages.extend({"role": turn.role, "content": turn.content} for turn in history)
    return messages
