"""Code, comments and tests speak of the candidate, never of one person as she or her.

Only the frozen version 1 fit checks wording in sourcer/llm.py keeps its old pronouns: it is never
sent, and stored score traces carry its hash (see tests/test_prompts.py)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSONAL = re.compile(r"\b(she|her|hers|herself|marina)\b", re.IGNORECASE)
FROZEN_START = "# Version 1 of the fit checks wording"
FROZEN_END = "# version: (system head, fit checks, edge checks, wants label, avoids label)"


def _without_frozen_v1(path: Path, text: str) -> str:
    if path.name != "llm.py" or FROZEN_START not in text:
        return text
    head, rest = text.split(FROZEN_START, 1)
    _, tail = rest.split(FROZEN_END, 1)
    return head + tail.split("\n", 3)[-1]  # the CHECKS_WORDING row for version 1 is frozen too


def test_no_personal_pronouns_in_code_comments_or_tests() -> None:
    found = []
    for path in sorted([*ROOT.glob("sourcer/**/*.py"), *ROOT.glob("tests/**/*.py")]):
        if path.name == Path(__file__).name:
            continue
        text = _without_frozen_v1(path, path.read_text())
        for n, line in enumerate(text.splitlines(), 1):
            if PERSONAL.search(line) and "GENDERED" not in line:
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not found, "say the candidate, not she or her:\n" + "\n".join(found)
