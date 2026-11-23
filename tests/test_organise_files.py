from pathlib import Path

import pytest

from src.organise_files import (
    EMPTY_NAME,
    MAX_NAME_BYTES,
    FileMove,
    make_plan,
    move_song,
    render_stem,
    sanitise_name,
    template_parts,
    template_problem,
    template_tags,
)
from src.track_metadata import TrackInfo


def test_sanitise_name():
    assert sanitise_name("AC/DC") == "AC_DC"
    assert sanitise_name('What?: "Yes" <no>') == "What__ _Yes_ _no_"
    assert sanitise_name("a\tb   c") == "a b c"
    assert sanitise_name("  .hidden. ") == "hidden"
    assert sanitise_name("") == EMPTY_NAME
    assert sanitise_name("...") == EMPTY_NAME


def test_sanitise_name_limits_bytes_without_breaking_characters():
    name = sanitise_name("é" * 500)
    assert len(name.encode("utf-8")) <= MAX_NAME_BYTES
    assert set(name) == {"é"}


@pytest.mark.parametrize(
    "template, problem_start",
    [
        ("", "Enter a naming template"),
        ("<album>/", "The template must end with a file name"),
        ("<foo>", "Unknown tag: <foo>"),
        ("<album/<title>", "A tag is missing its < or >"),
        ("<album>/static", "The file name needs at least one tag"),
    ],
)
def test_template_problems(template, problem_start):
    assert template_problem(template).startswith(problem_start)


def test_good_templates():
    assert template_problem("<albumartist>/<album>/<trackno> - <title>") is None
    assert template_problem("<Artist>/<Title>") is None


def test_template_parts_and_tags():
    template = " <artist>//<album>/<trackno> - <Title> <title> "
    assert template_parts(template) == ["<artist>", "<album>", "<trackno> - <Title> <title>"]
    assert template_tags(template) == ["artist", "album", "trackno", "title"]


def test_render_stem_fills_in_missing_values():
    values = {"artist": "AC/DC", "trackno": "", "title": ""}
    parts = ["<artist>", "<trackno> - <title>"]
    assert render_stem(parts, values, "file name") == "AC_DC/Unknown - file name"


def info(title, **fields):
    return TrackInfo(title, **fields)


def test_make_plan(tmp_path):
    rel_paths = ["a.mp3", "b.mp3", "Z/W.flac", "c.ogg"]
    infos = [
        info("Y", artist="X"),
        info("Y", artist="X"),
        info("W", artist="Z"),
        info("c", artist=""),
    ]
    titled = {"a.mp3", "b.mp3", "Z/W.flac"}
    plan = make_plan("<artist>/<title>", tmp_path, rel_paths, infos, titled, skip_missing=True)
    assert plan.unchanged == 1
    assert plan.rows == [
        FileMove(0, "a.mp3", "X/Y.mp3"),
        FileMove(1, "b.mp3", "X/Y (2).mp3"),
        FileMove(3, "c.ogg", "", ("artist", "title")),
    ]


def test_make_plan_without_skipping_uses_unknown_and_the_file_name(tmp_path):
    plan = make_plan(
        "<artist>/<title>", tmp_path, ["c.ogg"], [info("c")], set(), skip_missing=False
    )
    assert plan.rows == [FileMove(0, "c.ogg", "Unknown/c.ogg", ("artist", "title"))]


def test_make_plan_avoids_files_already_on_disk(tmp_path):
    (tmp_path / "X").mkdir()
    (tmp_path / "X" / "Y.mp3").write_bytes(b"")
    plan = make_plan(
        "<artist>/<title>", tmp_path, ["a.mp3"], [info("Y", artist="X")], {"a.mp3"}, False
    )
    assert plan.rows[0].new == "X/Y (2).mp3"


def test_move_song_brings_its_lyrics(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"song")
    (tmp_path / "a.lrc").write_text("[00:01.00]Hi", encoding="utf-8")
    move_song(tmp_path, FileMove(0, "a.mp3", "X/Y.mp3"))
    assert (tmp_path / "X" / "Y.mp3").read_bytes() == b"song"
    assert (tmp_path / "X" / "Y.lrc").exists()
    assert not (tmp_path / "a.mp3").exists()


def test_move_song_never_overwrites(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"song")
    (tmp_path / "b.mp3").write_bytes(b"other")
    with pytest.raises(FileExistsError):
        move_song(tmp_path, FileMove(0, "a.mp3", "b.mp3"))
    assert Path(tmp_path / "b.mp3").read_bytes() == b"other"
