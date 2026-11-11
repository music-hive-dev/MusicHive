"""Runs MusicHive's main.py inside the AppImage. AppRun starts this."""

import os
import runpy
import sys
from pathlib import Path

APPDIR = Path(__file__).resolve().parent.parent.parent.parent
MAIN = APPDIR / "usr" / "share" / "musichive" / "main.py"

# Programs MusicHive starts should get the system's libraries and Python, not
# the bundled ones. This process already has what it needs loaded or set up.
host_library_path = os.environ.pop("MUSICHIVE_HOST_LD_LIBRARY_PATH", None)
if host_library_path is None:
    os.environ.pop("LD_LIBRARY_PATH", None)
else:
    os.environ["LD_LIBRARY_PATH"] = host_library_path
for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"):
    os.environ.pop(name, None)

# MusicHive restarts itself with os.execv(sys.executable, [sys.executable, *sys.argv]).
# This makes that go through AppRun again, which sets everything up once more.
sys.executable = str(APPDIR / "usr" / "bin" / "musichive-restart")

sys.argv[0] = str(MAIN)
sys.path.insert(0, str(MAIN.parent))
runpy.run_path(str(MAIN), run_name="__main__")
