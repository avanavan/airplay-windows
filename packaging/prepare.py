"""Generate build inputs: the .ico and the Windows version resource. Prints the version.

  python packaging/prepare.py            -> writes build/airplay-windows.ico, build/version_info.txt
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from airplay_windows import APP_NAME, DISPLAY_NAME, __version__  # noqa: E402
from airplay_windows.ui.icon import write_ico  # noqa: E402

VERSION_INFO = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple}, prodvers={tuple}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{app}'),
      StringStruct('FileDescription', '{display}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{app}'),
      StringStruct('OriginalFilename', '{app}.exe'),
      StringStruct('ProductName', '{display}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    build = os.path.join(ROOT, "build")
    os.makedirs(build, exist_ok=True)
    write_ico(os.path.join(build, f"{APP_NAME}.ico"))
    parts = [int(p) for p in __version__.split(".")[:3] if p.isdigit()]
    numbers = tuple((parts + [0, 0, 0, 0])[:4])
    with open(os.path.join(build, "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(VERSION_INFO.replace("{tuple}", repr(numbers)).replace("{app}", APP_NAME)
                .replace("{display}", DISPLAY_NAME).replace("{version}", __version__))
    print(__version__)


if __name__ == "__main__":
    main()
