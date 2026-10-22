# PyInstaller spec: builds a self-contained CanAnalyser folder.
#   pyinstaller packaging/cananalyser.spec
# Kvaser's canlib32.dll is NOT bundled; it comes from the installed
# "Kvaser Drivers for Windows" on each PC.
from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    # python-can imports its interface backends by name at runtime.
    hiddenimports=[
        "can.interfaces.kvaser",
        "can.interfaces.kvaser.canlib",
        "can.interfaces.virtual",
    ],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CanAnalyser",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CanAnalyser")
