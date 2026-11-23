from src.lyrics import parse_lyrics


def test_plain_lyrics_drop_info_tags_and_leading_blank_lines():
    lyrics = parse_lyrics("\n\n[ti:Song]\nLine one\n\nLine two\n")
    assert lyrics.texts == ["Line one", "", "Line two"]
    assert lyrics.times_ms == []


def test_simple_timed_line():
    lyrics = parse_lyrics("[00:06.71]Gravity")
    assert lyrics.texts == ["Gravity"]
    assert lyrics.times_ms == [6710]


def test_colon_before_hundredths():
    assert parse_lyrics("[00:06:71]Gravity").times_ms == [6710]


def test_several_times_word_times_and_sorting():
    text = (
        "[ar:Someone]\n"
        "[00:03.00]Third\n"
        "[00:01.00][00:05.00]Hi\n"
        "[00:02.00]<00:02.10>Wo<00:02.50>rd\n"
    )
    lyrics = parse_lyrics(text)
    assert lyrics.texts == ["Hi", "Word", "Third", "Hi"]
    assert lyrics.times_ms == [1000, 2000, 3000, 5000]


def test_offset_moves_lines_earlier_but_not_below_zero():
    lyrics = parse_lyrics("[offset:+500]\n[00:00.20]A\n[00:01.00]B\n")
    assert lyrics.texts == ["A", "B"]
    assert lyrics.times_ms == [0, 500]


def test_negative_offset_moves_lines_later():
    lyrics = parse_lyrics("[offset:-250]\n[00:01.00]A\n")
    assert lyrics.times_ms == [1250]


def test_empty_text():
    lyrics = parse_lyrics("")
    assert lyrics.texts == []
    assert lyrics.times_ms == []
