#!/usr/bin/env python3
"""
Release build script for sql-doctor.

Run from the project root AFTER the CI ledger-integrity job has passed:
    python build.py           # builds both CLI and GUI binaries
    python build.py --cli     # CLI only (sql-doctor.exe)
    python build.py --gui     # GUI only (sql-doctor-gui.exe)

CLI steps (sql-doctor.exe via PyInstaller):
  1. Verify tests/coverage_ledger.json exists and is non-empty.
  2. Copy it to core/data/coverage_ledger.json (creates the dir if needed).
  3. Run PyInstaller against sql-doctor.spec.
  4. Smoke-check: dist/sql-doctor.exe ledger-status must exit 0.

GUI steps (sql-doctor-gui.exe via flet pack):
  5. Run flet pack on gui.py with the same skill + ledger data bundled.
     flet pack handles Flet's native renderer assets automatically.
  6. Smoke-check: launch dist/sql-doctor-gui.exe and confirm it starts
     without crashing (needs a display — a local release step, not CI).

Fails loudly and refuses to produce a release binary if any step fails.
End users never run this — they receive dist/ binaries directly.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEDGER_SRC = ROOT / "tests" / "coverage_ledger.json"
LEDGER_DST = ROOT / "core" / "data" / "coverage_ledger.json"
_EXT = ".exe" if sys.platform == "win32" else ""
CLI_EXE = ROOT / "dist" / f"sql-doctor{_EXT}"
GUI_EXE = ROOT / "dist" / f"sql-doctor-gui{_EXT}"


def die(msg: str) -> None:
    print(f"\nBUILD FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        die(f"command exited with code {result.returncode}")
    return result


def flet_cli() -> str:
    """
    Locate the `flet` console script.

    Flet 0.86 moved the CLI into a separate `flet-cli` package and dropped
    `python -m flet` (the `flet` package no longer has a `__main__`), so we must
    invoke the console script directly.
    """
    exe = shutil.which("flet")
    if exe:
        return exe
    scripts = Path(sys.executable).parent / ("Scripts" if sys.platform == "win32" else "bin")
    candidate = scripts / ("flet.exe" if sys.platform == "win32" else "flet")
    if candidate.exists():
        return str(candidate)
    die(
        "flet CLI not found on PATH or next to the interpreter.\n"
        "  Install it with: pip install flet-cli"
    )


def build_ledger_prereqs() -> None:
    """Steps 1–2: verify and stage the coverage ledger (shared by both builds)."""
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

    print("Step 2: copy ledger to core/data/")
    LEDGER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEDGER_SRC, LEDGER_DST)
    print(f"  OK — copied to {LEDGER_DST}\n")


def build_cli() -> None:
    """Steps 3–4: PyInstaller CLI build + ledger-status smoke check."""
    print("Step 3: PyInstaller CLI build (sql-doctor)")
    spec = ROOT / "sql-doctor.spec"
    if not spec.exists():
        die(f"Spec file not found: {spec}")
    run([sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"])
    print()

    print("Step 4: smoke check — ledger-status")
    if not CLI_EXE.exists():
        die(f"Built binary not found: {CLI_EXE}")
    result = subprocess.run([str(CLI_EXE), "ledger-status"], capture_output=True, text=True)
    print(f"  stdout: {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        die(
            f"{CLI_EXE.name} ledger-status exited with code {result.returncode} "
            "(expected 0 = OK).\n"
            "  The binary was built but the ledger is missing or corrupt inside it.\n"
            "  Do NOT distribute this build."
        )
    print(f"  OK — exit code 0\n")


def build_gui() -> None:
    """Steps 5–6: flet pack GUI build + startup smoke check."""
    print("Step 5: flet pack GUI build (sql-doctor-gui)")
    # flet pack handles Flet's native renderer and Flutter assets automatically.
    # --add-data bundles the skill YAML library and staged coverage ledger.
    # --hidden-import core.data ensures importlib.resources.files('core.data')
    # resolves correctly in the frozen binary (same requirement as the CLI build).
    #
    # Flet 0.86: invoke the `flet` console script (not `python -m flet`), and
    # --add-data takes `source:destination` (colon) on all platforms — the
    # flet-cli wrapper translates it to PyInstaller's native separator.
    run([
        flet_cli(), "pack", "gui.py",
        "--name", "sql-doctor-gui",
        "--add-data", "skills:skills",
        "--add-data", "core/data/coverage_ledger.json:core/data",
        "--hidden-import", "core.data",
        # -y: non-interactive. flet pack 0.86 otherwise prompts to delete the
        # existing build/ dir via input(), which crashes with EOFError when run
        # without a TTY (CI, or this build script).
        "-y",
    ])
    print()

    print("Step 6: smoke check — launches without crashing")
    if not GUI_EXE.exists():
        die(f"Built binary not found: {GUI_EXE}")
    # A Flet GUI has no --help; running it opens a window and blocks. So we
    # launch it, and treat "still alive after a few seconds" as success (it
    # started without crashing), then terminate it. An immediate non-zero exit
    # means it crashed on startup — a defective build. Note: this briefly opens
    # a window and therefore needs a display (it is a local release step, not CI).
    proc = subprocess.Popen([str(GUI_EXE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print("  OK — GUI launched and stayed alive (terminated after smoke test)\n")
    else:
        if proc.returncode != 0:
            stderr = (proc.stderr.read() or b"").decode(errors="replace").strip()
            print(f"  stderr: {stderr}", file=sys.stderr)
            die(
                f"{GUI_EXE.name} crashed on startup (exit {proc.returncode}).\n"
                "  Do NOT distribute this build."
            )
        print("  OK — GUI exited cleanly on startup\n")


def main() -> None:
    args = sys.argv[1:]
    do_cli = "--gui" not in args
    do_gui = "--cli" not in args

    print("=== sql-doctor release build ===\n")

    build_ledger_prereqs()

    if do_cli:
        build_cli()
    if do_gui:
        build_gui()

    built = []
    if do_cli:
        built.append(str(CLI_EXE))
    if do_gui:
        built.append(str(GUI_EXE))
    print(f"=== Build successful ===")
    for b in built:
        print(f"  {b}")


if __name__ == "__main__":
    main()
