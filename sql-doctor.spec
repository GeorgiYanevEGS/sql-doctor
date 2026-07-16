# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for sql-doctor (one-file build).
#
# Before running PyInstaller directly, use build.py instead — it copies the
# CI-verified tests/coverage_ledger.json into core/data/ first and runs the
# post-build smoke check.
#
# Manual build (only if you know what you're doing):
#   python build.py

a = Analysis(
    ["cli.py"],
    pathex=[],
    binaries=[],
    datas=[
        # Skill YAML library — bundled as-is at the bundle root.
        ("skills", "skills"),
        # Coverage ledger — copied here by build.py before PyInstaller runs.
        # In source checkout this file does not exist; always run build.py.
        ("core/data/coverage_ledger.json", "core/data"),
    ],
    # core.data is not imported directly anywhere, so PyInstaller won't collect
    # it automatically. Listing it here ensures __init__.py is bundled and
    # importlib.resources.files('core.data') works in the frozen binary.
    hiddenimports=["core.data"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the binary lean: test tooling has no place in an end-user build.
    excludes=["pytest", "_pytest", "unittest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sql-doctor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
