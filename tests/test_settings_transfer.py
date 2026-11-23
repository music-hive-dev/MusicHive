import pytest
from PySide6.QtCore import QSettings

from src.settings_transfer import MARKER_KEY, export_settings, import_settings


def ini(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


def test_export_leaves_out_private_settings(tmp_path):
    source = ini(tmp_path / "app.ini")
    source.setValue("theme", "dark")
    source.setValue("volume", 50)
    source.setValue("lastfm/username", "me")
    source.setValue("lastfm/sessionKey", "secret")
    source.setValue("dataFolder", "/somewhere")
    source.setValue("discordCoverLinks/abc", "https://example.com")

    export_settings(source, tmp_path / "out.ini")
    exported = ini(tmp_path / "out.ini")
    assert sorted(exported.allKeys()) == sorted([MARKER_KEY, "theme", "volume"])


def test_export_then_import(tmp_path):
    source = ini(tmp_path / "app.ini")
    source.setValue("theme", "dark")
    source.setValue("volume", 50)
    export_settings(source, tmp_path / "out.ini")

    target = ini(tmp_path / "other.ini")
    target.setValue("theme", "light")
    assert import_settings(target, tmp_path / "out.ini") == 2
    assert target.value("theme") == "dark"
    assert int(target.value("volume")) == 50
    assert not target.contains(MARKER_KEY)


def test_import_ignores_private_settings_in_the_file(tmp_path):
    file = tmp_path / "handmade.ini"
    file.write_text(
        f"[General]\n{MARKER_KEY}=1\ntheme=light\n\n[lastfm]\nusername=intruder\n",
        encoding="utf-8",
    )
    target = ini(tmp_path / "app.ini")
    assert import_settings(target, file) == 1
    assert not target.contains("lastfm/username")


def test_import_refuses_other_files(tmp_path):
    file = tmp_path / "other.ini"
    file.write_text("[General]\ntheme=light\n", encoding="utf-8")
    with pytest.raises(ValueError):
        import_settings(ini(tmp_path / "app.ini"), file)
