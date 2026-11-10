"""Dark and light mode, and the highlight colour.

The stylesheets and palette in the .ui files, and the icons in assets/, are
made for dark mode with the default highlight colour. At startup, setup()
reads the chosen mode and highlight colour, and the colours are swapped as
each window is loaded (apply) and in a themed copy of the icons (assets_dir).
A change is used the next time MusicHive starts.
"""

import re
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QDir, QSettings, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

ROOT_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT_DIR / "assets"

MODE_KEY = "interface/mode"
HIGHLIGHT_KEY = "interface/highlight"
MODES = ("dark", "light")
DEFAULT_MODE = "dark"
SOURCE_HIGHLIGHT = "#6c53a6"  # the highlight colour in the .ui files and assets/
DEFAULT_HIGHLIGHTS = {"dark": SOURCE_HIGHLIGHT, "light": "#96dc82"}  # unless one is chosen

TEXT = "#ffffff"  # text and icons in the .ui files and assets/
PANEL = "#222023"  # the docks, tabs and dialogs
DIMMED_ALPHA = 170  # less important text: 67% opacity

# The colours the .ui files use, and what they become in light mode.
LIGHT_COLOURS = {
    TEXT: "#1d1b20",  # text and icons
    "#151416": "#ece9f0",  # window frame, menu bar, status bar
    PANEL: "#f8f7fa",  # docks, tabs, dialogs, menus
    "#312d34": "#ddd7e3",  # buttons, fields, outlines, alternate rows
    "#433d47": "#cdc5d6",  # hover, slider fill, scrollbar handles
}

_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
_BLOCK = re.compile(r"\{[^{}]*\}")
_SELECTION_COLOR = re.compile(r"(selection-color\s*:\s*)#ffffff\b", re.IGNORECASE)
_HIGHLIGHT_BACKGROUND = re.compile(
    rf"(?<![\w-])background(-color)?\s*:\s*{SOURCE_HIGHLIGHT}\b", re.IGNORECASE
)

_mode = DEFAULT_MODE
_highlight = SOURCE_HIGHLIGHT
_assets_dir = ASSETS_DIR
_ui_dirs: dict[Path, Path] = {}  # .ui folder -> folder the loader resolves icons from


# ----- the settings -----


def valid_highlight(text: str) -> str | None:
    """`text` as a #rrggbb colour in lower case, or None if it is not one."""
    text = text.strip()
    if not text.startswith("#"):
        text = f"#{text}"
    return text.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else None


def saved_mode(settings: QSettings) -> str:
    mode = str(settings.value(MODE_KEY, DEFAULT_MODE))
    return mode if mode in MODES else DEFAULT_MODE


def default_highlight(mode: str) -> str:
    return DEFAULT_HIGHLIGHTS.get(mode, SOURCE_HIGHLIGHT)


def saved_highlight(settings: QSettings) -> str:
    """The chosen highlight colour, or the default one of the chosen mode."""
    default = default_highlight(saved_mode(settings))
    chosen = settings.value(HIGHLIGHT_KEY)
    return (valid_highlight(str(chosen)) if chosen is not None else None) or default


def save_highlight(settings: QSettings, colour: str) -> None:
    """Save a highlight colour. The mode's default is not saved, so the
    highlight goes on following the mode until another colour is chosen."""
    if colour == default_highlight(saved_mode(settings)):
        settings.remove(HIGHLIGHT_KEY)
    else:
        settings.setValue(HIGHLIGHT_KEY, colour)


def active_mode() -> str:
    """The mode this run of MusicHive uses."""
    return _mode


def active_highlight() -> str:
    """The highlight colour this run of MusicHive uses."""
    return _highlight


def is_default() -> bool:
    return _mode == DEFAULT_MODE and _highlight == SOURCE_HIGHLIGHT


# ----- colours -----


def _contrast(a: QColor, b: QColor) -> float:
    def luminance(color: QColor) -> float:
        def channel(value: float) -> float:
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        return (
            0.2126 * channel(color.redF())
            + 0.7152 * channel(color.greenF())
            + 0.0722 * channel(color.blueF())
        )

    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def on_highlight() -> str:
    """Text colour on the highlight colour: white, or dark on a light highlight."""
    dark = LIGHT_COLOURS[TEXT]
    highlight = QColor(_highlight)
    white_contrast = _contrast(highlight, QColor(TEXT))
    return TEXT if white_contrast >= _contrast(highlight, QColor(dark)) else dark


def colour(dark_colour: str) -> str:
    """What a colour from the .ui files becomes in the active mode."""
    key = dark_colour.lower()
    if key == SOURCE_HIGHLIGHT:
        return _highlight
    if _mode == "light":
        return LIGHT_COLOURS.get(key, key)
    return key


def dimmed_text_on_panel() -> str:
    """Less important text over a panel, as a solid colour (rich text needs one)."""
    text, panel = QColor(colour(TEXT)), QColor(colour(PANEL))
    share = DIMMED_ALPHA / 255
    mixed = QColor(
        round(text.red() * share + panel.red() * (1 - share)),
        round(text.green() * share + panel.green() * (1 - share)),
        round(text.blue() * share + panel.blue() * (1 - share)),
    )
    return mixed.name()


# ----- stylesheets and palettes -----


def _restyle_block(block: str) -> str:
    # Text in a rule whose background is the highlight colour sits on it.
    # (Not selection-background-color: that rule's own text is on the normal background.)
    on_highlight_rule = _HIGHLIGHT_BACKGROUND.search(block) is not None
    block = _SELECTION_COLOR.sub(lambda m: m.group(1) + on_highlight(), block)

    def hex_colour(match: re.Match) -> str:
        value = match.group(0).lower()
        if value == TEXT and on_highlight_rule:
            return on_highlight()
        return colour(value)

    def rgba_colour(match: re.Match) -> str:
        red, green, blue, alpha = (int(part) for part in match.groups())
        mapped = QColor(colour(QColor(red, green, blue).name()))
        return f"rgba({mapped.red()}, {mapped.green()}, {mapped.blue()}, {alpha})"

    block = _HEX.sub(hex_colour, block)
    return _RGBA.sub(rgba_colour, block)


def restyle(sheet: str) -> str:
    """A stylesheet from a .ui file, in the active mode and highlight colour."""
    if is_default() or not sheet:
        return sheet
    sheet = sheet.replace("url(assets/", "url(assets:")  # the themed icons
    if "{" not in sheet:
        return _restyle_block(sheet)
    return _BLOCK.sub(lambda m: _restyle_block(m.group(0)), sheet)


def repalette(palette: QPalette) -> QPalette:
    """A palette from a .ui file, in the active mode and highlight colour."""
    palette = QPalette(palette)
    roles = QPalette.ColorRole
    themed = (
        roles.WindowText, roles.Button, roles.Light, roles.Midlight, roles.Dark, roles.Mid,
        roles.Text, roles.BrightText, roles.ButtonText, roles.Base, roles.Window,
        roles.Highlight, roles.HighlightedText, roles.AlternateBase,
        roles.PlaceholderText, roles.Accent,
    )  # not the tooltip colours or the shadow
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role in themed:
            old = palette.color(group, role)
            if role in (roles.Highlight, roles.Accent):
                new = QColor(_highlight)
            elif role == roles.HighlightedText:
                new = QColor(on_highlight())
            elif _mode == "light":
                name = old.name()
                if name in LIGHT_COLOURS:
                    new = QColor(LIGHT_COLOURS[name])
                else:  # shades the .ui files don't use elsewhere: flip light and dark
                    hue, saturation, lightness, _alpha = old.getHslF()
                    new = QColor.fromHslF(max(hue, 0.0), saturation, 1.0 - lightness)
                new.setAlpha(old.alpha())
            else:
                continue
            palette.setColor(group, role, new)
    return palette


def apply(root: QWidget) -> None:
    """Theme a window just loaded from a .ui file, and everything in it."""
    if is_default():
        return
    for widget in [root, *root.findChildren(QWidget)]:
        if widget.testAttribute(Qt.WidgetAttribute.WA_SetPalette):
            widget.setPalette(repalette(widget.palette()))
        sheet = widget.styleSheet()
        if sheet:
            widget.setStyleSheet(restyle(sheet))


# ----- icons -----


def assets_dir() -> Path:
    """The folder to load icons from: assets/, or a copy in the active colours."""
    return _assets_dir


def asset_path(name: str) -> Path:
    return _assets_dir / name


def ui_dir(folder: Path) -> Path:
    """The folder a .ui file loader works from, so "../assets/x.svg" finds the themed icons."""
    return _ui_dirs.get(folder, folder)


def _themed_copy(target: Path) -> None:
    """Write the icons to `target`/assets in the active colours."""
    icons = target / "assets"
    if target.exists():
        shutil.rmtree(target)
    icons.mkdir(parents=True)
    (target / "ui").mkdir()
    text = colour(TEXT)
    for svg in ASSETS_DIR.glob("*.svg"):
        content = svg.read_text(encoding="utf-8")
        content = re.sub(r"#ffffff\b", text, content, flags=re.IGNORECASE)
        content = re.sub(SOURCE_HIGHLIGHT, _highlight, content, flags=re.IGNORECASE)
        (icons / svg.name).write_text(content, encoding="utf-8")
    # The .ui files also show the app's logo from the folder above them.
    for logo in ROOT_DIR.glob("musichive*.svg"):
        shutil.copy2(logo, target / logo.name)


def setup(settings: QSettings, ui_folder: Path) -> None:
    """Read the chosen mode and highlight colour. Call once, after the
    QApplication exists and before any window is loaded."""
    global _mode, _highlight, _assets_dir
    _mode = saved_mode(settings)
    _highlight = saved_highlight(settings)
    if not is_default():
        cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        target = Path(cache or tempfile.gettempdir()) / "theme"
        try:
            _themed_copy(target)
            _assets_dir = target / "assets"
            _ui_dirs[ui_folder] = target / "ui"
        except OSError as error:
            print(f"MusicHive: cannot write the themed icons to {target}: {error}")
    QDir.setSearchPaths("assets", [str(_assets_dir)])  # for url(assets:...) in the stylesheets
