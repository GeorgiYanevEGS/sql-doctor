"""
Drift guard for SKILLS.md.

SKILLS.md is generated from skills/*.yaml by scripts/generate_skills_doc.py.
This test regenerates the catalog in-memory and asserts the committed file
matches — so adding or changing a skill without regenerating fails CI, the
same way the README test-count meta-test prevents that number from drifting.
"""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from core.skill_matcher import load_skills  # noqa: E402
from generate_skills_doc import render  # noqa: E402


def test_skills_doc_is_current():
    expected = render(load_skills().skills)
    actual = (_REPO / "SKILLS.md").read_text(encoding="utf-8")
    assert actual == expected, (
        "SKILLS.md is out of date. Regenerate it with:\n"
        "    python scripts/generate_skills_doc.py\n"
        "and commit the result."
    )
