import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


SCRIPT_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SCRIPT_DIR.parents[2]
VECTOR60_DISTRIBUTIONS = ("vtracer", "skia-pathops", "svgpathtools")
VECTOR60_METADATA = []
for distribution in VECTOR60_DISTRIBUTIONS:
    VECTOR60_METADATA += copy_metadata(distribution)
MODEL_CONTRACT_SCHEMA_DATA = [
    (str(path), "model_contracts/schemas")
    for path in sorted((REPO_ROOT / "model_contracts" / "schemas").glob("*.json"))
]
TARGET_TRIPLE = os.environ.get("STARBRIDGE_SIDECAR_TARGET_TRIPLE", "")
if TARGET_TRIPLE:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+", TARGET_TRIPLE)
        or TARGET_TRIPLE not in {"aarch64-apple-darwin", "x86_64-apple-darwin"}
    ):
        raise ValueError("Unsupported STARBRIDGE_SIDECAR_TARGET_TRIPLE.")
    CONTENTS_DIRECTORY = f"_internal-{TARGET_TRIPLE}"
else:
    CONTENTS_DIRECTORY = "_internal"

analysis = Analysis(
    [str(SCRIPT_DIR / "sidecar_entry.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[*VECTOR60_METADATA, *MODEL_CONTRACT_SCHEMA_DATA],
    hiddenimports=[
        "model_contracts.schemas",
        "starbridge_mcp.backend",
        "starbridge_mcp.mcp_server",
        "starbridge_mcp.server",
        "starbridge_mcp.vectorization.engine",
        "starbridge_mcp.vectorization.presets",
        "starbridge_mcp.vectorization.svg_verify",
        "starbridge_mcp.vectorization.artisan_edit",
        "starbridge_mcp.vectorization.artisan",
        "starbridge_mcp.vectorization.artisan_strokes",
        "starbridge_mcp.vectorization.curve_geometry",
        "vtracer",
        "pathops",
        "svgpathtools",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "pytest",
        "starbridge_mcp.vectorization.app",
        "starbridge_mcp.vectorization.gui",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="starbridge-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    contents_directory=CONTENTS_DIRECTORY,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="starbridge-sidecar",
)
