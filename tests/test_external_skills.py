"""
Tests for the external skills override folder.

A user who only has the packaged .exe (not the source repo) can drop extra
skill YAMLs into %APPDATA%\\sql-doctor\\skills\\. load_skills() merges them with
the bundled library when given an external_skills_dir. Contract:

- external scanning is OPT-IN (an explicit parameter) so tests and
  generate_skills_doc.py stay reproducible and never pick up machine-local files;
- a name collision with a bundled skill is a hard error (never a silent
  override — that would let a changed skill inherit the frozen ledger's
  "verified" entries and be falsely reported as SKILL_CLEARED);
- an externally-added skill has no ledger entry, so it is UNVERIFIED — it can
  fire positive matches but never contributes a verified "clean" verdict.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.skill_matcher import (
    DEFAULT_SKILLS_DIR,
    DuplicateSkillNameError,
    LedgerStatus,
    default_external_skills_dir,
    ensure_external_skills_dir,
    load_skills,
    match_skills,
)


def _write_skill(folder: Path, name: str, node_type: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.yaml"
    path.write_text(
        f"""name: {name}
covers_node_types: ["{node_type}"]
severity: low
description: External test skill {name}.
detects:
  node_type: "{node_type}"
explanation: External test skill.
fix_template: Do the thing.
""",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# load_skills merge behaviour
# ---------------------------------------------------------------------------

def test_external_skill_is_merged(tmp_path):
    ext = tmp_path / "skills"
    _write_skill(ext, "my_custom_check", "Custom Scan")

    loaded = load_skills(external_skills_dir=ext)
    names = {s.name for s in loaded.skills}

    assert "my_custom_check" in names
    # Bundled skills are still present alongside the external one.
    assert "missing_index" in names


def test_external_collision_with_bundled_name_raises(tmp_path):
    ext = tmp_path / "skills"
    # Reuse a real bundled skill name — must be rejected, not silently merged.
    _write_skill(ext, "missing_index", "Seq Scan")

    with pytest.raises(DuplicateSkillNameError) as exc:
        load_skills(external_skills_dir=ext)
    assert "missing_index" in str(exc.value)


def test_external_skill_is_unverified_against_bundled_ledger(tmp_path):
    ext = tmp_path / "skills"
    _write_skill(ext, "my_custom_check", "Custom Scan")

    # With a real ledger applied, the external skill has no ledger entry, so its
    # coverage of "Custom Scan" must NOT be verified.
    from core.skill_matcher import DEFAULT_LEDGER_PATH

    loaded = load_skills(ledger_path=DEFAULT_LEDGER_PATH, external_skills_dir=ext)
    assert loaded.ledger_status == LedgerStatus.OK
    ext_skill = next(s for s in loaded.skills if s.name == "my_custom_check")
    assert ext_skill.is_verified_for("Custom Scan") is False, (
        "an externally-added skill must be UNVERIFIED — it has no ledger entry"
    )


def test_no_external_dir_is_backward_compatible():
    # The default (no external dir) must behave exactly as before.
    baseline = load_skills()
    with_none = load_skills(external_skills_dir=None)
    assert {s.name for s in baseline.skills} == {s.name for s in with_none.skills}


def test_nonexistent_external_dir_is_ignored(tmp_path):
    missing = tmp_path / "does_not_exist"
    loaded = load_skills(external_skills_dir=missing)
    # No crash, bundled skills load normally.
    assert any(s.name == "missing_index" for s in loaded.skills)


def test_external_skill_can_fire_a_match(tmp_path):
    # A merged external skill participates in matching like any bundled one.
    ext = tmp_path / "skills"
    _write_skill(ext, "flag_all_custom_scans", "Custom Scan")
    loaded = load_skills(external_skills_dir=ext)

    plan = parse_explain_json([{
        "Plan": {
            "Node Type": "Custom Scan",
            "Plan Rows": 1, "Actual Rows": 1,
            "Total Cost": 1.0, "Actual Total Time": 0.1,
        },
        "Planning Time": 0.1, "Execution Time": 0.2,
    }])
    diag = match_skills(plan, loaded.skills, table_row_counts={}, ledger_status=loaded.ledger_status)
    assert any(m.skill_name == "flag_all_custom_scans" for m in diag.matches)


# ---------------------------------------------------------------------------
# external folder scaffolding
# ---------------------------------------------------------------------------

def test_default_external_skills_dir_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = default_external_skills_dir()
    assert d == tmp_path / "sql-doctor" / "skills"


def test_ensure_external_skills_dir_creates_dir_and_readme(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = ensure_external_skills_dir()

    assert d.is_dir()
    readme = d / "README.md"
    assert readme.exists()
    content = readme.read_text(encoding="utf-8")
    assert "YAML" in content
    assert "CONTRIBUTING" in content


def test_ensure_external_skills_dir_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    ensure_external_skills_dir()
    # A user-edited README must not be clobbered on a second call.
    readme = default_external_skills_dir() / "README.md"
    readme.write_text("my notes", encoding="utf-8")
    ensure_external_skills_dir()
    assert readme.read_text(encoding="utf-8") == "my notes"
