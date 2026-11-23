from src.song_settings import SoundPresets, chosen_presets


def test_nothing_set():
    assert chosen_presets(None, None) == SoundPresets()


def test_song_only():
    song = SoundPresets("Bass", "Bars")
    assert chosen_presets(song, None) == SoundPresets("Bass", "Bars")


def test_playlist_wins_over_song():
    song = SoundPresets("Bass", "Bars")
    playlist = SoundPresets("Vocal", "")
    assert chosen_presets(song, playlist) == SoundPresets("Vocal", "Bars")


def test_song_can_override_playlist():
    song = SoundPresets("Bass", "", over_playlist=True)
    playlist = SoundPresets("Vocal", "Wave")
    assert chosen_presets(song, playlist) == SoundPresets("Bass", "Wave")


def test_is_empty():
    assert SoundPresets().is_empty
    assert not SoundPresets(over_playlist=True).is_empty
    assert not SoundPresets(visualiser="Bars").is_empty
