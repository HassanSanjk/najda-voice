"""Question-repeat guard tests — run with: python tests/test_question_repeat.py

Covers the question-repeat guard (see kb_loader.detect_question_stuck):
extraction of questions from assistant turns, clustering of reworded repeats
(a caller who kept failing to answer was asked the same question 4x,
reworded each time), the hard-override ask-count gate, window scoping, the
prompt soft nudge, and the escalation-phrase lookup the override speaks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.prompts import kb_loader

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(name)
    print(f"  [{status}] {name}{' — ' + detail if detail else ''}")


def test_extract_questions() -> None:
    print("=" * 60)
    print("TEST 1: _extract_questions")
    print("=" * 60)
    check(
        "splits an english question out of a longer reply",
        kb_loader._extract_questions("Is your leg yellow? Tell me what you see.")
        == ["Is your leg yellow?"],
    )
    check(
        "splits multiple questions in one turn",
        kb_loader._extract_questions("هل لون رجلك أصفر؟ ولون يدك؟")
        == ["هل لون رجلك أصفر؟", "ولون يدك؟"],
    )
    check(
        "ignores statements",
        kb_loader._extract_questions("Call emergency services now. Do not stop.") == [],
    )
    check(
        "ignores escalation-only replies (no question mark)",
        kb_loader._extract_questions("اتصل بالإسعاف الآن.") == [],
    )


def test_reworded_repeat_clusters_and_overrides() -> None:
    print("=" * 60)
    print("TEST 2: Aug-14 rewording sequence clusters; hard override fires")
    print("=" * 60)
    # The motivating case: the same question, reworded each time, 4x.
    assistant_texts = [
        "هل لون رجلك أصفر؟",
        "لون رجلك شاحب أو مصفر؟",
        "هل رجلك لونها أصفر؟",
        "أخبرني وش لون رجلك؟",
    ]
    stuck = kb_loader.detect_question_stuck(assistant_texts)
    check("returns a result", stuck is not None)
    if stuck:
        # count is measured from the LATEST wording's perspective, so
        # transitive rewording drift can undercount (Q2/Q3 don't cluster
        # directly with Q4). That doesn't matter: the gate is count >= 2
        # (latest wording matches ANY prior ask), and TEST 3 proves the
        # 3rd ask is already intercepted on that basis.
        check("latest wording matches a prior ask", stuck["count"] >= 2,
              f"count={stuck['count']}")
        check("hard override fires (3rd ask intercepted)", stuck["overrides"] is True)
        check("keeps the latest wording for the fallback", "لون رجلك" in stuck["question"])


def test_ask_count_gating() -> None:
    print("=" * 60)
    print("TEST 3: ask-count gating")
    print("=" * 60)
    # One ask: no override yet; the soft nudge fires (count >= 1) to prevent
    # the 2nd ask from being generated.
    stuck1 = kb_loader.detect_question_stuck(["هل لون رجلك أصفر؟"])
    check("first ask: count 1", stuck1 is not None and stuck1["count"] == 1)
    check("first ask: no override", stuck1 is not None and stuck1["overrides"] is False)
    # Second clustered ask: override now fires — the next generation (the
    # would-be 3rd ask) is the one that gets cut off.
    stuck2 = kb_loader.detect_question_stuck([
        "هل لون رجلك أصفر؟",
        "لون رجلك شاحب أو مصفر؟",
    ])
    check("second ask: count 2", stuck2 is not None and stuck2["count"] == 2)
    check("second ask: override fires", stuck2 is not None and stuck2["overrides"] is True)


def test_window_scoping() -> None:
    print("=" * 60)
    print("TEST 4: window scoping — stale repeats don't count")
    print("=" * 60)
    # Q1 and Q2 are pushed out of the 6-turn window by filler turns; the
    # latest turn's question is then treated as fresh.
    assistant_texts = [
        "هل لون رجلك أصفر؟",
        "لون رجلك شاحب أو مصفر؟",
        "ضع يدك على الجرح.",
        "اضغط بقوة.",
        "ارفع الطرف.",
        "اتصل بالإسعاف الآن.",
        "ماذا يحدث الآن؟",
        "هل تستطيع سماعي؟",
        "هل رجلك لونها أصفر؟",
    ]
    stuck = kb_loader.detect_question_stuck(assistant_texts)
    check("old repeats fall outside the window", stuck is not None and stuck["count"] == 1,
          f"count={stuck['count'] if stuck else None}")
    check("no override for the fresh question", stuck is not None and stuck["overrides"] is False)


def test_no_question_no_signal() -> None:
    print("=" * 60)
    print("TEST 5: turns without a question produce no signal")
    print("=" * 60)
    check("empty history", kb_loader.detect_question_stuck([]) is None)
    check("greeting has no question", kb_loader.detect_question_stuck(["Hi, I'm Najda. Tell me what happened."]) is None)
    check("escalation phrase is not a question", kb_loader.detect_question_stuck(["اتصل بالإسعاف الآن. لا تتوقف."]) is None)


def test_cross_question_overlap_is_loose_by_design() -> None:
    print("=" * 60)
    print("TEST 6: cross-question overlap (accepted tradeoff)")
    print("=" * 60)
    # Two DIFFERENT questions about the same symptom share the content
    # skeleton and cluster at 0.67 — token-level matching can't tell
    # "reworded" from "different body part". This is why the guard's real
    # protection is the ASK-COUNT gate (>= 2 asks), not the matcher: a
    # single such pair cannot override anything on its own.
    overlap = kb_loader._question_overlap("هل لون رجلك أصفر؟", "هل لون يدك أصفر؟")
    check("leg/arm questions cluster loosely", overlap >= kb_loader.QUESTION_SIMILARITY_THRESHOLD,
          f"overlap={overlap:.2f}")


def test_prompt_soft_nudge() -> None:
    print("=" * 60)
    print("TEST 7: format_kb_for_prompt soft-nudge block")
    print("=" * 60)
    nudged = kb_loader.format_kb_for_prompt("KB_Bleeding.yaml", "en", stuck_question="Is your leg yellow?")
    check("nudge block present when stuck_question passed", "do NOT ask it again" in nudged)
    plain = kb_loader.format_kb_for_prompt("KB_Bleeding.yaml", "en")
    check("no nudge block without stuck_question", "do NOT ask it again" not in plain)


def test_escalation_phrase_lookup() -> None:
    print("=" * 60)
    print("TEST 8: get_escalation_phrase for the hard override")
    print("=" * 60)
    check(
        "english escalation phrase",
        kb_loader.get_escalation_phrase("KB_CPR.yaml", "en")
        == "Call emergency services now. Do not stop what you are doing.",
    )
    check(
        "arabic escalation phrase",
        kb_loader.get_escalation_phrase("KB_CPR.yaml", "ar")
        == "اتصل بالإسعاف الآن. لا تتوقف عما تفعله.",
    )


def main() -> None:
    test_extract_questions()
    test_reworded_repeat_clusters_and_overrides()
    test_ask_count_gating()
    test_window_scoping()
    test_no_question_no_signal()
    test_cross_question_overlap_is_loose_by_design()
    test_prompt_soft_nudge()
    test_escalation_phrase_lookup()

    print("=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("RESULT: ALL PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
