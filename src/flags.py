"""The flags the visualiser's bars can be coloured as, read from JSON files.

Each flag is one file; its name without ".json" is how the settings refer to it.
The flags that come with MusicHive are in assets/flags/. The user's own go in
the "flags" folder of the data folder; one with the same file name as a built-in
flag replaces it. A file looks like this:

    {
      "name": "Pride",
      "stripes": ["#E40303", "#FF8C00", "#FFED00", "#008026", "#24408E", "#732982"]
    }

The stripes go top to bottom on the flag, and become the bars left to right.
A colour listed twice is a stripe twice as wide. "order" (a number) puts a flag
earlier in the list; flags without one come after, by name.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QColor

from src.theme import ASSETS_DIR

BUILT_IN_DIR = ASSETS_DIR / "flags"
USER_DIR_NAME = "flags"
README_NAME = "README.txt"
MAX_STRIPES = 100

README_TEXT = """Your own flags for the visualiser's bars (Settings, Now Playing, Colours).

Put one .json file per flag in this folder, then open the settings window again.
For example, "sunset.json":

{
  "name": "Sunset",
  "stripes": ["#FF5E5B", "#FF9F45", "#FFD166", "#3D348B"]
}

The stripes go top to bottom on the flag, and become the bars left to right.
A colour listed twice is a stripe twice as wide. "order" (a number) puts a flag
earlier in the list. A file named like a built-in flag (pride.json, say)
replaces that flag. The built-in ones are in MusicHive's assets/flags folder.
"""


@dataclass(frozen=True)
class Flag:
    id: str  # the file name without ".json"
    name: str  # as the colour lists show it
    stripes: tuple[str, ...]  # top to bottom
    order: float  # earlier in the list when lower


_flags: dict[str, Flag] | None = None


def user_flags_dir() -> Path:
    from src.library_db import data_dir  # here, so that this module loads on its own

    return data_dir() / USER_DIR_NAME


def _read_flag(path: Path) -> Flag | None:
    """A flag file, or None if it is not one (a problem is never fatal)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stripes = data.get("stripes")
    if not isinstance(stripes, list) or not 0 < len(stripes) <= MAX_STRIPES:
        return None
    if not all(isinstance(stripe, str) and QColor(stripe).isValid() for stripe in stripes):
        return None
    name = data.get("name")
    order = data.get("order")
    return Flag(
        path.stem,
        name.strip() if isinstance(name, str) and name.strip() else path.stem,
        tuple(QColor(stripe).name() for stripe in stripes),
        float(order) if isinstance(order, (int, float)) and not isinstance(order, bool) else float("inf"),
    )


def _read_folder(folder: Path) -> dict[str, Flag]:
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return {}
    found = {}
    for path in paths:
        flag = _read_flag(path)
        if flag is not None:
            found[flag.id] = flag
    return found


def reload_flags() -> dict[str, Flag]:
    """Read the flag files again (the user may have added some)."""
    global _flags
    merged = {**_read_folder(BUILT_IN_DIR), **_read_folder(user_flags_dir())}
    ordered = sorted(merged.values(), key=lambda flag: (flag.order, flag.name.casefold()))
    _flags = {flag.id: flag for flag in ordered}
    return _flags


def all_flags() -> dict[str, Flag]:
    """Every flag by id, in the order the colour lists show them."""
    return _flags if _flags is not None else reload_flags()


def flag_stripes(flag_id: str) -> tuple[str, ...] | None:
    flag = all_flags().get(flag_id)
    return flag.stripes if flag is not None else None


def prepare_user_flags_dir() -> Path:
    """Make the user's flags folder, with a note on how to add flags. Raises OSError."""
    folder = user_flags_dir()
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / README_NAME
    if not readme.exists():
        readme.write_text(README_TEXT, encoding="utf-8")
    return folder
