# PyInstaller spec: one folder with airplay-windows.exe (windowed) and airplay-windows-cli.exe (console).
# Build with scripts/build.ps1, which runs packaging/prepare.py first for the icon and version resource.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
BUILD = os.path.join(ROOT, "build")
ICON = os.path.join(BUILD, "airplay-windows.ico")
VERSION = os.path.join(BUILD, "version_info.txt")

common = dict(
    pathex=[os.path.join(ROOT, "src")],
    # soundcard loads its COM declarations (.h files) at runtime through cffi.
    datas=collect_data_files("soundcard"),
    # zeroconf is partly compiled; pystray picks its backend at runtime.
    hiddenimports=collect_submodules("zeroconf") + ["pystray._win32"],
    excludes=["pytest", "PyInstaller"],
)

app = Analysis([os.path.join(SPECPATH, "entry_app.py")], **common)
cli = Analysis([os.path.join(SPECPATH, "entry_cli.py")], **common)

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="airplay-windows",
              console=False, icon=ICON, version=VERSION)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="airplay-windows-cli",
              console=True, icon=ICON, version=VERSION)

COLLECT(app_exe, app.binaries, app.datas, cli_exe, cli.binaries, cli.datas, name="airplay-windows")
