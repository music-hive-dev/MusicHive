import json
import math

import pytest

from src import flags


def write_flag(folder, flag_id, data):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{flag_id}.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    """The user's flags folder, in a temporary place, with nothing read yet."""
    folder = tmp_path / "flags"
    monkeypatch.setattr(flags, "user_flags_dir", lambda: folder)
    monkeypatch.setattr(flags, "_flags", None)
    return folder


def test_every_built_in_flag_is_valid():
    paths = sorted(flags.BUILT_IN_DIR.glob("*.json"))
    assert paths
    for path in paths:
        assert flags._read_flag(path) is not None, path.name


def test_read_flag(tmp_path):
    path = write_flag(
        tmp_path, "sunset", {"name": " Sunset ", "order": 3, "stripes": ["#FF5E5B", "#ff9f45"]}
    )
    assert flags._read_flag(path) == flags.Flag("sunset", "Sunset", ("#ff5e5b", "#ff9f45"), 3.0)


def test_read_flag_defaults(tmp_path):
    path = write_flag(tmp_path, "plain", {"stripes": ["red"], "order": True})
    flag = flags._read_flag(path)
    assert flag.name == "plain"
    assert flag.stripes == ("#ff0000",)
    assert math.isinf(flag.order)


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        [1, 2],
        {"stripes": []},
        {"stripes": "#ff0000"},
        {"stripes": ["#ff0000", "not a colour"]},
        {"stripes": ["#ff0000", 5]},
        {"stripes": ["#ff0000"] * (flags.MAX_STRIPES + 1)},
    ],
)
def test_read_flag_rejects_bad_files(tmp_path, data):
    assert flags._read_flag(write_flag(tmp_path, "bad", data)) is None


def test_user_flags_replace_built_in_ones_and_sort_last(user_dir):
    write_flag(user_dir, "pride", {"name": "My Pride", "stripes": ["#000000"]})
    write_flag(user_dir, "zz", {"name": "Aardvark", "stripes": ["#ffffff"]})
    all_flags = flags.reload_flags()
    assert flags.flag_stripes("pride") == ("#000000",)
    ids = list(all_flags)
    # Both have no "order", so they come after the built-in ones, by name.
    assert ids[-2:] == ["zz", "pride"]


def test_unknown_flag(user_dir):
    assert flags.flag_stripes("no such flag") is None


def test_prepare_user_flags_dir_keeps_an_existing_readme(user_dir):
    assert flags.prepare_user_flags_dir() == user_dir
    readme = user_dir / flags.README_NAME
    assert readme.read_text(encoding="utf-8") == flags.README_TEXT
    readme.write_text("mine", encoding="utf-8")
    flags.prepare_user_flags_dir()
    assert readme.read_text(encoding="utf-8") == "mine"
