from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

analysis = Analysis(
    [str(ROOT / "app" / "runner.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files("app"),
    hiddenimports=collect_submodules("app"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="autoflow-runner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
