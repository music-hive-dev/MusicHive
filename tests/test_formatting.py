from src.formatting import format_long_duration, format_size, format_time


def test_format_time_minutes_and_seconds():
    assert format_time(0) == "0:00"
    assert format_time(59_999) == "0:59"
    assert format_time(61_000) == "1:01"


def test_format_time_with_hours():
    assert format_time(3_661_000) == "1:01:01"


def test_format_time_negative_is_zero():
    assert format_time(-5000) == "0:00"


def test_format_size_bytes():
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(-1) == "0 B"


def test_format_size_larger_units():
    assert format_size(1000) == "1 KB"
    assert format_size(1500) == "1.5 KB"
    assert format_size(66_600_000_000) == "66.6 GB"


def test_format_size_stops_at_terabytes():
    assert format_size(5 * 1000**5) == "5000 TB"


def test_format_long_duration_under_a_day():
    assert format_long_duration(90 * 60_000) == "1:30"
    assert format_long_duration(89_999) == "0:01"


def test_format_long_duration_with_days():
    assert format_long_duration((7 * 1440 + 83) * 60_000) == "7d 1:23"


def test_format_long_duration_with_seconds():
    assert format_long_duration(3_723_000, seconds=True) == "1:02:03"
    assert format_long_duration((86_400 + 3_723) * 1000, seconds=True) == "1d 1:02:03"
