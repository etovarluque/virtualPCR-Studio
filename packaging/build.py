"""Build the Windows release: a folder with virtualPCR Studio.exe, zipped.

    python packaging/build.py

PyInstaller freezes the app into dist/virtualPCR Studio/ (one folder rather
than one file: a single-file exe unpacks ~200 MB to a temp folder on every
start). Beside the exe go what the app looks for there — jar/ and an empty
output/ — and the licence, notice, README and user guide. The folder is then
zipped as dist/virtualPCR-Studio-<version>-win64.zip. Java is not bundled:
the app finds an installed one and links to a download when there is none.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from vpcr import APP_NAME, __version__  # noqa: E402

DIST, WORK = ROOT / "dist", ROOT / "build"
APP_DIR = DIST / APP_NAME
EXTRAS = ["README.md", "LICENSE", "NOTICE", "docs"]
# Imported by the GUI only at run time, or not at all: kept out of the bundle.
EXCLUDE = ["tkinter", "matplotlib", "IPython", "pytest", "PyQt6.QtWebEngineCore",
           "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.Qt3DCore", "PyQt6.QtMultimedia"]


def main() -> int:
    shutil.rmtree(APP_DIR, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP_NAME, "--distpath", str(DIST), "--workpath", str(WORK),
           "--specpath", str(WORK), "--paths", str(ROOT),
           "--collect-submodules", "primer3", "--collect-data", "primer3",
           *(a for m in EXCLUDE for a in ("--exclude-module", m)),
           str(ROOT / "packaging" / "launcher.py")]
    subprocess.run(cmd, check=True)

    shutil.copytree(ROOT / "jar", APP_DIR / "jar", dirs_exist_ok=True)
    (APP_DIR / "output").mkdir(exist_ok=True)
    for name in EXTRAS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, APP_DIR / name, dirs_exist_ok=True)
        elif src.exists():
            shutil.copy2(src, APP_DIR / name)

    zip_path = DIST / f"virtualPCR-Studio-{__version__}-win64.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(APP_DIR.rglob("*")):
            z.write(f, Path(APP_NAME) / f.relative_to(APP_DIR))
    print(f"\n{APP_DIR}\n{zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
