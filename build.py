#!/usr/bin/env python3
"""
Release build script for sql-doctor.

Run from the project root AFTER the CI ledger-integrity job has passed:
    python build.py

Steps:
  1. Verify tests/coverage_ledger.json exists and is non-empty.
  2. Copy it to core/data/coverage_ledger.json (creates the dir if needed).
  3. Run PyInstaller against sql-doctor.spec.
  4. Run the built binary's ledger-status command and assert exit code 0.

Fails loudly and refuses to produce a release binary if any step fails.
End users never run this — they receive dist/sql-doctor.exe directly.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER_SRC = ROOT / "tests" / "coverage_ledger.json"
LEDGER_DST = ROOT / "core" / "data" / "coverage_ledger.json"
EXE_NAME = "sql-doctor.exe" if sys.platform == "win32" else "sql-doctor"
EXE = ROOT / "dist" / EXE_NAME


def die(msg: str) -> None:
    print(f"\nBUILD FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        die(f"command exited with code {result.returncode}")
    return result


def main() -> None:
    print("=== sql-doctor release build ===\n")

    # Step 1: verify source ledger
    print("Step 1: verify source ledger")
    if not LEDGER_SRC.exists():
        die(
            f"Source ledger not found: {LEDGER_SRC}\n"
            "  Run: pytest tests/test_coverage_ledger.py  (then commit the result)"
        )
    size = LEDGER_SRC.stat().st_size
    if size == 0:
        die(f"Source ledger is empty: {LEDGER_SRC}")
    print(f"  OK — {LEDGER_SRC} ({size} bytes)\n")

    # Step 2: copy to core/data/
    print("Step 2: copy ledger to core/data/")
    LEDGER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEDGER_SRC, LEDGER_DST)
    print(f"  OK — copied to {LEDGER_DST}\n")

    # Step 3: run PyInstaller
    print("Step 3: PyInstaller build")
    spec = ROOT / "sql-doctor.spec"
    if not spec.exists():
        die(f"Spec file not found: {spec}")
    run([sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"])
    print()

    # Step 4: smoke check
    print("Step 4: smoke check — ledger-status")
    if not EXE.exists():
        die(f"Built binary not found: {EXE}")
    result = subprocess.run([str(EXE), "ledger-status"], capture_output=True, text=True)
    print(f"  stdout: {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        die(
            f"{EXE_NAME} ledger-status exited with code {result.returncode} "
            f"(expected 0 = OK).\n"
            "  The binary was built but the ledger is missing or corrupt inside it.\n"
            "  Do NOT distribute this build."
        )
    print(f"  OK — exit code 0\n")

    print(f"=== Build successful: {EXE} ===")


if __name__ == "__main__":
    main()
