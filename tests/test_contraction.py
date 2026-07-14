"""Quick test for contraction normalization and single-word matching."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.prompts.kb_loader import match_scenario, _normalize

print("Normalization:")
print(f"  can't breathe -> {_normalize('can' + chr(39) + 't breathe')}")
print(f"  cannot breathe -> {_normalize('cannot breathe')}")
print()

tests = [
    ("she cannot breathe", "en", "KB_Choking.yaml"),
    ("my arm is broken", "en", "KB_Fractures.yaml"),
    ("a snake bit my leg", "en", "KB_SnakeBites.yaml"),
    ("my child is choking", "en", "KB_Choking.yaml"),
    ("his heart stopped beating", "en", "KB_CPR.yaml"),
    ("I got an electric shock", "en", "KB_ElectricShock.yaml"),
    ("my allergy is acting up", "en", "KB_AllergicReactions.yaml"),
]

for text, lang, expected in tests:
    result = match_scenario(text, lang)
    status = "PASS" if result == expected else "FAIL"
    print(f"  [{status}] {text!r} -> {result} (expected {expected})")
