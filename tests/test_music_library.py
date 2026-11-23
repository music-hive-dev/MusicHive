from src.music_library import find_audio_files


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_missing_folder_gives_nothing(tmp_path):
    assert find_audio_files(tmp_path / "nope") == []


def test_finds_supported_files_sorted_and_skips_hidden(tmp_path):
    for name in (
        "b/Song.MP3",
        "a/z.flac",
        "B/x.ogg",
        "top.wav",
        "notes.txt",
        ".hidden.mp3",
        ".secret/in.mp3",
    ):
        touch(tmp_path / name)
    found = [path.relative_to(tmp_path).as_posix() for path in find_audio_files(tmp_path)]
    # Folder and file names compare ignoring case, so b/ and B/ sort as one folder.
    assert found == ["a/z.flac", "b/Song.MP3", "B/x.ogg", "top.wav"]


def test_not_recursive(tmp_path):
    touch(tmp_path / "top.mp3")
    touch(tmp_path / "sub" / "deep.mp3")
    assert find_audio_files(tmp_path, recursive=False) == [tmp_path / "top.mp3"]
