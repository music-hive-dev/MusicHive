import pytest

from src.playlists import MAX_NAME_LENGTH, Playlist, PlaylistStore


@pytest.fixture
def store(tmp_path):
    return PlaylistStore(tmp_path / "playlists")


def test_no_folder_means_no_playlists(store):
    assert store.names() == []
    assert store.folders() == []


def test_save_and_load_keep_everything(store):
    playlist = Playlist(
        "Road trip",
        ["a.mp3", "b/ç.flac"],
        locked=True,
        description="Long drives",
        filter_kind="album_artist",
        filter_value="Someone",
        folder="Summer",
        rules=[{"tag": "genre", "comparison": "is", "value": "Rock"}],
        match_any=True,
        no_duplicates=True,
        limit={"amount": 25, "unit": "songs", "order": "newest"},
        equaliser="Bass",
        visualiser="Bars",
    )
    store.save(playlist)
    assert store.load("Road trip") == playlist
    assert store.names() == ["Road trip"]


def test_load_of_a_missing_or_broken_file_is_empty(store):
    assert store.load("Nope") == Playlist("Nope")
    store.folder.mkdir()
    (store.folder / "Broken.json").write_text("{not json", encoding="utf-8")
    assert store.load("Broken") == Playlist("Broken")
    (store.folder / "List.json").write_text("[1, 2]", encoding="utf-8")
    assert store.load("List") == Playlist("List")


def test_load_ignores_values_of_the_wrong_type(store):
    store.folder.mkdir()
    (store.folder / "Odd.json").write_text(
        '{"tracks": ["a.mp3", 3], "locked": "yes", "description": 5, "rules": "x"}',
        encoding="utf-8",
    )
    playlist = store.load("Odd")
    assert playlist.tracks == ["a.mp3"]
    assert not playlist.locked
    assert playlist.description == ""
    assert not playlist.is_auto


def test_auto_and_read_only():
    assert not Playlist("a").read_only
    assert Playlist("a", locked=True).read_only
    auto = Playlist("a", rules=[])
    assert auto.is_auto
    assert auto.read_only


def test_names_are_sorted_ignoring_case(store):
    for name in ("beta", "Alpha", "gamma"):
        store.save(Playlist(name))
    assert store.names() == ["Alpha", "beta", "gamma"]


@pytest.mark.parametrize(
    "name, message_start",
    [
        ("", "The name cannot be empty"),
        ("   ", "The name cannot be empty"),
        ("x" * (MAX_NAME_LENGTH + 1), "The name can be at most"),
        ("a/b", "The name cannot contain a slash"),
        (".hidden", "The name cannot contain a slash"),
        ("mix", "A playlist with that name already exists"),
    ],
)
def test_check_name_problems(store, name, message_start):
    store.save(Playlist("Mix"))
    assert store.check_name(name).startswith(message_start)


def test_check_name_allows_the_name_being_renamed(store):
    store.save(Playlist("Mix"))
    assert store.check_name("mix", ignore="Mix") is None
    assert store.check_name("New") is None


def test_copy_name(store):
    assert store.copy_name("Mix") == "Mix copy"
    store.save(Playlist("Mix copy"))
    assert store.copy_name("Mix") == "Mix copy 2"


def test_copy_name_stays_within_the_length_limit(store):
    name = store.copy_name("x" * MAX_NAME_LENGTH)
    assert len(name) == MAX_NAME_LENGTH
    assert name.endswith(" copy")


def test_rename_duplicate_delete(store):
    store.save(Playlist("Mix", ["a.mp3"]))
    store.icon_file("Mix").write_bytes(b"png")

    store.rename("Mix", "Party")
    assert store.names() == ["Party"]
    assert store.icon_file("Party").read_bytes() == b"png"
    assert not store.icon_file("Mix").exists()

    store.duplicate("Party", "Party copy")
    assert store.load("Party copy").tracks == ["a.mp3"]
    assert store.icon_file("Party copy").exists()

    store.delete("Party")
    assert store.names() == ["Party copy"]
    assert not store.icon_file("Party").exists()


def test_animated_icon(store, tmp_path):
    store.save(Playlist("Mix"))
    gif = tmp_path / "source.gif"
    gif.write_bytes(b"GIF89a")
    assert store.animated_icon("Mix") is None
    store.set_animated_icon("Mix", gif)
    assert store.animated_icon("Mix").read_bytes() == b"GIF89a"
    store.set_animated_icon("Mix", None)
    assert store.animated_icon("Mix") is None


def test_add_tracks_skips_songs_already_present(store):
    store.save(Playlist("Mix", ["a", "b", "c"]))
    assert store.add_tracks("Mix", ["d", "a", "d"], before="b") == 1
    assert store.load("Mix").tracks == ["a", "d", "b", "c"]
    assert store.add_tracks("Mix", ["e"]) == 1
    assert store.load("Mix").tracks[-1] == "e"


def test_move_tracks(store):
    store.save(Playlist("Mix", ["a", "b", "c"]))
    store.move_tracks("Mix", ["c", "a"], before="b")
    assert store.load("Mix").tracks == ["c", "a", "b"]
    store.move_tracks("Mix", ["c"])
    assert store.load("Mix").tracks == ["a", "b", "c"]


def test_remove_tracks(store):
    store.save(Playlist("Mix", ["a", "b", "c"]))
    assert store.remove_tracks("Mix", ["b", "x"]) == 1
    assert store.load("Mix").tracks == ["a", "c"]


def test_remove_and_rename_tracks_everywhere(store):
    store.save(Playlist("One", ["a", "b"]))
    store.save(Playlist("Two", ["b", "c"]))
    store.remove_track_everywhere("b")
    assert store.load("One").tracks == ["a"]
    assert store.load("Two").tracks == ["c"]
    store.rename_tracks({"a": "new/a", "c": "new/c"})
    assert store.load("One").tracks == ["new/a"]
    assert store.load("Two").tracks == ["new/c"]


def test_folders(store):
    store.create_folder("Summer")
    store.create_folder("autumn")
    assert store.folders() == ["autumn", "Summer"]
    assert store.check_folder_name("summer").startswith("A folder with that name")

    store.save(Playlist("Mix"))
    store.set_folder("Mix", "Summer")
    store.rename_folder("Summer", "Hot")
    assert store.load("Mix").folder == "Hot"
    assert store.folders() == ["autumn", "Hot"]

    store.delete_folder("Hot")
    assert store.load("Mix").folder == ""
    assert store.folders() == ["autumn"]
    assert store.names() == ["Mix"]  # folders.list is not a playlist


def test_setters(store):
    store.save(Playlist("Mix"))
    store.set_description("Mix", "Hello")
    store.set_presets("Mix", "Bass", "Bars")
    store.set_filter("Mix", "genre", "Rock")
    store.set_locked("Mix", True)
    store.set_rules("Mix", [], True, True, {"amount": 1, "unit": "songs", "order": "random"})
    playlist = store.load("Mix")
    assert playlist.description == "Hello"
    assert (playlist.equaliser, playlist.visualiser) == ("Bass", "Bars")
    assert (playlist.filter_kind, playlist.filter_value) == ("genre", "Rock")
    assert playlist.locked
    assert playlist.rules == []
    assert playlist.match_any and playlist.no_duplicates
    assert playlist.limit["amount"] == 1
