"""Translations: the language MusicHive speaks, chosen in Settings > Interface.

Texts in the Python code go through tr(); the .ui files are translated by
QUiLoader as they load. Both come from translations/musichive_<code>.qm, which
translations/update.sh makes from the .ts files people translate in Qt
Linguist. Qt's own texts (standard buttons, the menu of a text field) come
from the qtbase_<code>.qm files that come with Qt.

Texts with a number are written like "%n song(s)" or "%n cop(y/ies)": a
translation gives a form for each plural of its language, and without one tr()
makes the English singular or plural from the brackets.

The language is set once, before any window is loaded; changing it takes a restart.
"""

import re
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QSettings, QTranslator

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"
LANGUAGE_KEY = "language"
SYSTEM = ""  # follow the system's language
ENGLISH = "en"  # the language the texts are written in

_active = ENGLISH
_translators: list[QTranslator] = []  # kept, or Python frees them while in use
_CHOICE = re.compile(r"\((\w*)/(\w*)\)")  # (y/ies): the singular and the plural ending
_S = re.compile(r"(?<=\w)\(s\)")  # song(s)


def tr(text: str, disambiguation: str | None = None, n: int = -1) -> str:
    """`text` in the chosen language.

    `text` must be a plain string literal, never an f-string, so that lupdate can
    find it: put the changing parts in with .format() afterwards. With `n`, "%n"
    stands for the number and the translation can have a form for each plural.
    `disambiguation` tells apart the same English words that mean different things.
    """
    translated = QCoreApplication.translate("", text, disambiguation, n)
    if n >= 0 and translated == text.replace("%n", str(n)):  # not translated
        return english_plural(translated, n != 1)
    return translated


def english_plural(text: str, plural: bool) -> str:
    """ "song(s)" as "song" or "songs", "cop(y/ies)" as "copy" or "copies"."""
    text = _CHOICE.sub(lambda match: match.group(2 if plural else 1), text)
    return _S.sub("s" if plural else "", text)


def available() -> list[tuple[str, str]]:
    """(code, name) of each language there is a translation for, English first,
    each named in its own language."""
    languages = [(ENGLISH, "English")]
    for path in sorted(TRANSLATIONS_DIR.glob("musichive_*.qm")):
        code = path.stem.removeprefix("musichive_")
        name = QLocale(code).nativeLanguageName() or code
        languages.append((code, name[:1].upper() + name[1:]))
    return languages


def saved(settings: QSettings) -> str:
    return str(settings.value(LANGUAGE_KEY, SYSTEM))


def active() -> str:
    """The language in use since the start."""
    return _active


def setup(settings: QSettings) -> None:
    """Use the saved language. Call before any window is loaded."""
    global _active
    wanted = saved(settings)
    codes = [code for code, _name in available()]
    if wanted == SYSTEM:
        # The first of the system's languages there is a translation for.
        wanted = ENGLISH
        for name in QLocale.system().uiLanguages():
            name = name.replace("-", "_")
            for candidate in (name, name.split("_")[0]):
                if candidate in codes:
                    wanted = candidate
                    break
            else:
                continue
            break
    elif wanted not in codes:  # its translation is gone
        wanted = ENGLISH
    _active = wanted
    if wanted == ENGLISH:
        return
    own = QTranslator(QCoreApplication.instance())
    if own.load(str(TRANSLATIONS_DIR / f"musichive_{wanted}.qm")):
        QCoreApplication.installTranslator(own)
        _translators.append(own)
    locale = QLocale(wanted)
    QLocale.setDefault(locale)  # numbers and dates written as there, too
    qt = QTranslator(QCoreApplication.instance())
    if qt.load(locale, "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        QCoreApplication.installTranslator(qt)
        _translators.append(qt)


def QT_TR_NOOP(text: str) -> str:  # noqa: N802  (the name lupdate looks for)
    """Marks `text` for translation without translating it yet, for texts made
    before the language is set (when modules load); pass them through tr() where
    they are shown."""
    return text
