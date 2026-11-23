from pathlib import Path

from src.m3u import M3uTrack, read_m3u, write_m3u


def test_write_uses_relative_paths_inside_the_folder(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    playlist = folder / "list.m3u"
    outside = tmp_path / "other" / "b.mp3"
    write_m3u(
        playlist,
        [
            M3uTrack(folder / "a.mp3", 200, "Song  A\n"),
            M3uTrack(outside, -1, "B"),
        ],
    )
    assert playlist.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        "#EXTINF:200,Song A",
        "a.mp3",
        "#EXTINF:-1,B",
        str(outside),
    ]


def test_write_then_read_gives_the_same_paths(tmp_path):
    playlist = tmp_path / "list.m3u8"
    paths = [tmp_path / "sub" / "één.flac", Path("/elsewhere/x.mp3")]
    write_m3u(playlist, [M3uTrack(path, 1, path.stem) for path in paths])
    assert read_m3u(playlist) == paths


def test_read_skips_comments_blank_lines_and_web_addresses(tmp_path):
    playlist = tmp_path / "list.m3u"
    playlist.write_text(
        "#EXTM3U\n\n#EXTINF:10,x\nhttp://radio.example/stream\nsong.mp3\n", encoding="utf-8"
    )
    assert read_m3u(playlist) == [tmp_path / "song.mp3"]


def test_read_file_urls_are_decoded(tmp_path):
    playlist = tmp_path / "list.m3u"
    playlist.write_text("file:///music/a%20b.mp3\n", encoding="utf-8")
    assert read_m3u(playlist) == [Path("/music/a b.mp3")]


def test_read_windows_paths(tmp_path):
    playlist = tmp_path / "list.m3u"
    playlist.write_text("sub\\song.mp3\n", encoding="utf-8")
    assert read_m3u(playlist) == [tmp_path / "sub" / "song.mp3"]


def test_read_relative_paths_are_normalised(tmp_path):
    folder = tmp_path / "lists"
    folder.mkdir()
    playlist = folder / "list.m3u"
    playlist.write_text("../x.mp3\n", encoding="utf-8")
    assert read_m3u(playlist) == [tmp_path / "x.mp3"]


def test_read_latin1_and_bom(tmp_path):
    legacy = tmp_path / "old.m3u"
    legacy.write_bytes(b"caf\xe9.mp3\n")
    assert read_m3u(legacy) == [tmp_path / "café.mp3"]

    with_bom = tmp_path / "bom.m3u8"
    with_bom.write_bytes(b"\xef\xbb\xbfsong.mp3\n")
    assert read_m3u(with_bom) == [tmp_path / "song.mp3"]
