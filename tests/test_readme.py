"""
Meta-test: the test count stated in README.md must match the number of
tests pytest actually collects.

This is the narrowest, most mechanically checkable claim in the README —
a specific integer that should equal the output of `pytest --collect-only`.
Allowing it to drift silently is the same class of error the coverage ledger
exists to prevent, applied one layer up at the documentation.

The test counts itself: when pytest collects this file, test_readme_test_count
is included, so the README total must reflect that.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_test_count_matches_reality():
    """README's stated test count must equal the number pytest collects."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "--no-header"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    count_match = re.search(r"(\d+) tests? collected", result.stdout)
    assert count_match, (
        f"Could not parse test count from pytest --collect-only output:\n{result.stdout}"
    )
    actual = int(count_match.group(1))

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_match = re.search(r"(\d+) tests:", readme)
    assert readme_match, (
        "Could not find 'N tests:' pattern in README.md status section. "
        "Expected a line like 'CLI wiring, 37 tests:'"
    )
    stated = int(readme_match.group(1))

    assert stated == actual, (
        f"README says '{stated} tests' but pytest collected {actual}. "
        "Update the count in README.md (search for 'N tests:' in the status section)."
    )
